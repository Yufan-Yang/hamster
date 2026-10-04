#!/opt/grabber/venv/bin/python
"""拾光 (grabber): a home download box for the Pi. The code is in the shiguang package; this starts a process:

    grabber.py web            the page and API (waitress)
    grabber.py worker         runs queued jobs, follows uploaders, the Pi's task workers, housekeeping
    grabber.py run-job N      one download/analysis job (started by the worker)
    grabber.py task N WORKER  one CPU task a Pi worker claimed (started by the worker)
"""
import sys

import shiguang

if __name__ == "__main__":
    shiguang.main.main(sys.argv)
else:
    # `import grabber as G` (refresh_metadata.py, one-off scripts): G.anything finds it in whichever module has it
    def __getattr__(name):
        for mod in (shiguang.core, shiguang.llm, shiguang.download, shiguang.library, shiguang.search, shiguang.board,
                    shiguang.tasks, shiguang.notes, shiguang.ask, shiguang.usage, shiguang.pipeline, shiguang.telegram,
                    shiguang.channels, shiguang.web, shiguang.main):
            if hasattr(mod, name):
                return getattr(mod, name)
        raise AttributeError(name)
