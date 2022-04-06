"""
Startup script files to be run after ipython is initialized via:
``c.InteractiveTerminalApp.exec_lines``

Code here will take executed after both IPython has been loaded and
the namespace has been populated.
"""

import logging

import IPython
from nabs.callbacks import ELogPoster
from prompt_toolkit.keys import Keys

logger = logging.getLogger(__name__)

try:
    # minimize namespace pollution
    IPython.get_ipython().pt_app.key_bindings.remove(
        Keys.ControlBackslash
    )
except Exception as exc:
    logger.debug(exc, exc_info=True)

try:
    # RE, ELog will already exist by now
    elogc = ELogPoster(elog, IPython.get_ipython())  # noqa
    RE.subscribe(elogc)  # noqa
except Exception as exc:
    logger.debug(exc, exc_info=True)
