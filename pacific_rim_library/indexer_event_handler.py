# pylint: disable=missing-docstring

from watchdog.events import PatternMatchingEventHandler


class IndexerEventHandler(PatternMatchingEventHandler):
    """Event handler for the PRL indexer."""

    def __init__(self, prl_indexer, exceptions_queue, **kwargs):
        super().__init__(**kwargs)
        self.prl_indexer = prl_indexer
        self.exceptions_queue = exceptions_queue

    def on_modified(self, event):
        """Update the corresponding record in PRL."""
        try:
            self.prl_indexer.update_record(event.src_path)
        except Exception as e:
            self.exceptions_queue.put(e)

    def on_deleted(self, event):
        """Delete the corresponding record in PRL."""
        try:
            self.prl_indexer.remove_record(event.src_path)
        except Exception as e:
            self.exceptions_queue.put(e)
