# pylint: disable=missing-docstring

from watchdog.events import FileSystemEventHandler


class IndexerEventHandler(FileSystemEventHandler):
    """Event handler for the PRL indexer."""

    def __init__(self, prl_indexer):
        self.prl_indexer = prl_indexer

    def on_modified(self, event):
        """Update the corresponding record in PRL."""
        if not event.is_directory:
            self.prl_indexer.update_record(event.src_path)

    def on_deleted(self, event):
        """Delete the corresponding record in PRL."""
        if not event.is_directory:
            self.prl_indexer.remove_record(event.src_path)
