# pylint: disable=missing-docstring

from watchdog.events import PatternMatchingEventHandler


class IndexerEventHandler(PatternMatchingEventHandler):
    """Event handler for the PRL indexer."""

    def __init__(self, prl_indexer, **kwargs):
        super().__init__(**kwargs)
        self.prl_indexer = prl_indexer

    def on_modified(self, event):
        """Update the corresponding record in PRL."""
        self.prl_indexer.update_record(event.src_path)

    def on_deleted(self, event):
        """Delete the corresponding record in PRL."""
        self.prl_indexer.remove_record(event.src_path)
