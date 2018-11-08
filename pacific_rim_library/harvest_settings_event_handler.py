# pylint: disable=missing-docstring

import os

from watchdog.events import FileSystemEventHandler


class HarvestSettingsEventHandler(FileSystemEventHandler):
    """Event handler for the jOAI harvester settings file."""

    def __init__(self, prl_indexer):
        self.prl_indexer = prl_indexer

    def on_modified(self, event):
        """Update the harvester settings."""
        if not event.is_directory and os.path.basename(event.src_path) == self.prl_indexer.config['leveldb']['harvester_settings']['source']['files']['scheduled_harvests']:
            self.prl_indexer.set_harvester_settings()
