import collections
import gevent
from gevent import event

from . import exceptions


class QueryObserver(object):
    """
    A query observer observes a specific queryset for changes and propagates these
    changes to all interested subscribers.
    """

    STATUS_NEW = 'new'
    STATUS_INITIALIZING = 'initializing'
    STATUS_OBSERVING = 'observing'
    STATUS_STOPPED = 'stopped'

    def __init__(self, pool, queryset):
        """
        Creates a new query observer.

        :param pool: QueryObserverPool instance
        :param queryset: A QuerySet that should be observed
        """

        self.status = QueryObserver.STATUS_NEW
        self._pool = pool
        self._queryset = queryset.all()
        self._query = queryset.query.sql_with_params()
        self._last_results = collections.OrderedDict()
        self._subscribers = set()
        self._dependencies = set()

        # Ensure that the target model is registered with a specific serializer.
        self._serializer = pool.get_serializer(self._queryset.model)

    def add_dependency(self, table):
        """
        Registers a new dependency for this query observer.

        :param table: Name of the dependent database table
        """

        if table in self._dependencies:
            return

        self._dependencies.add(table)
        self._pool.register_dependency(self, table)

    @property
    def stopped(self):
        """
        True if the query observer has been stopped.
        """

        return self.status == QueryObserver.STATUS_STOPPED

    def evaluate(self, return_full=True):
        """
        Evaluates the query observer and checks if there have been any changes. This function
        may yield.

        :param return_full: True if the full set of rows should be returned
        """

        if self.status == QueryObserver.STATUS_STOPPED:
            raise exceptions.ObserverStopped

        # Be sure to handle status changes before any yields, so that the other greenlets
        # will see the changes and will be able to wait on the initialization future.
        if self.status == QueryObserver.STATUS_INITIALIZING:
            self._initialization_future.wait()
        elif self.status == QueryObserver.STATUS_NEW:
            self._initialization_future = event.Event()
            self.status = QueryObserver.STATUS_INITIALIZING

            # Determine which tables this query depends on.
            for table in self._queryset.query.tables:
                self.add_dependency(table)
            self.add_dependency(self._queryset.model._meta.db_table)

        # Evaluate the query (this operation yields).
        # TODO: Only compute difference between old and new, ideally on the SQL server using hashes.
        new_results = collections.OrderedDict()
        primary_key = self._queryset.model._meta.pk.name
        # We need to make a copy of the queryset by calling .all() as otherwise, the results will be
        # cached inside the queryset and the query will not be executed on subsequent runs.
        results = self._serializer(self._queryset.all(), many=True).data
        if self.status == QueryObserver.STATUS_STOPPED:
            return []

        for row in results:
            new_results[row[primary_key]] = row

        # Process difference between old results and new results.
        added = []
        changed = []
        removed = []
        for row_id, row in self._last_results.iteritems():
            if row_id not in new_results:
                removed.append(row)

        for row_id, row in new_results.iteritems():
            if row_id not in self._last_results:
                added.append(row)
            else:
                old_row = self._last_results[row_id]
                if row != old_row:
                    changed.append((old_row, row))

        self._last_results = new_results

        if self.status == QueryObserver.STATUS_INITIALIZING:
            self.status = QueryObserver.STATUS_OBSERVING
            future = self._initialization_future
            self._initialization_future = None
            future.set()
        elif self.status == QueryObserver.STATUS_OBSERVING:
            self.emit(added, changed, removed)

        if return_full:
            return self._last_results.values()

    def emit(self, added, changed, removed):
        """
        Notifies all subscribers about query changes.

        :param added: A list of rows there were added
        :param changed: A list of rows that were changed
        :param removed: A list of rows that were removed
        """

        for subscriber in self._subscribers:
            # TODO
            print "would emit to %s: added=%s changed=%s removed=%s" % (subscriber, added, changed, removed)

    def subscribe(self, subscriber):
        """
        Adds a new subscriber.
        """

        self._subscribers.add(subscriber)

    def unsubscribe(self, subscriber):
        """
        Unsubscribes a specific subscriber to this query observer. If no subscribers
        are left, this query observer is stopped.
        """

        self._subscribers.pop(subscriber)
        if not self._subscribers:
            self.stop()

    def stop(self):
        """
        Stops this query observer.
        """

        self.status = QueryObserver.STATUS_STOPPED

        # Unregister all dependencies.
        for dependency in self._dependencies:
            self._pool.unregister_dependency(self, dependency)

    def __eq__(self, other):
        return self._query == other._query

    def __hash__(self):
        return hash(self._query)