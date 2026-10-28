"""Record real entries for functions sharing coverage's module entry line."""

import json
import os
from pathlib import Path
import sys
import threading

from coverage import Coverage, CoveragePlugin


class EntryRecorder(CoveragePlugin):
    def __init__(self):
        self.directory = None
        self.seen = set()
        self.lock = threading.Lock()

    def configure(self, config):
        if self.directory is not None:
            return
        if sys.getprofile() is not None or threading.getprofile() is not None:
            raise ValueError("Function-entry measurement cannot replace an existing profiler.")
        self.directory = Path(os.environ["QUALITY_ENTRIES_DIR"])
        self.output_path().touch()
        sys.setprofile(self.record)
        threading.setprofile(self.record)

    def output_path(self):
        return self.directory / f"entries-{os.getpid()}.jsonl"

    def record(self, frame, event, arg):
        if event != "call":
            return
        code = frame.f_code
        if code.co_firstlineno != 1 or code.co_name == "<module>":
            return
        if Coverage.current() is not None:
            self.save_call(code.co_filename, code.co_name)

    def save_call(self, filename, name):
        entry = (os.path.normcase(os.path.realpath(filename)), name)
        with self.lock:
            if entry not in self.seen:
                with self.output_path().open("a", encoding="utf-8") as output:
                    output.write(json.dumps(entry) + "\n")
                self.seen.add(entry)


_recorder = EntryRecorder()


def coverage_init(reg, options):
    reg.add_configurer(_recorder)
