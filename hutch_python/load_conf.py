"""
This module is responsible for reading and interpreting the ``conf.yml`` file.
The file's specification can be found on the `yaml_files` page.
"""
import logging
import yaml
from pathlib import Path

from bluesky import RunEngine
from bluesky.callbacks.best_effort import BestEffortCallback
from bluesky.utils import install_kicker
from pcdsdevices.mv_interface import setup_preset_paths

from . import plan_defaults
from .cache import LoadCache
from .constants import VALID_KEYS
from .daq import get_daq_objs
from .exp_load import get_exp_objs
from .happi import get_happi_objs, get_lightpath
from .namespace import class_namespace, metadata_namespace
from .qs_load import get_qs_objs
from .user_load import get_user_objs
from .utils import (get_current_experiment, safe_load, hutch_banner,
                    count_ns_leaves)

logger = logging.getLogger(__name__)


def load(cfg=None):
    """
    Read the config file and convert the yaml format into a ``dict``.

    This method:

    - Finds the hutch's launch directory
    - Loads the hutch's objects by calling `load_conf.load_conf`

    Parameters
    ----------
    cfg: ``str``, optional
        Path to the ``conf.yml`` file.
        If this is missing, we'll end up with a very empty environment.

    Returns
    -------
    objs: ``dict{str: object}``
        All objects defined by ``conf.yml``. The strings are the names
        that will be accessible in the global namespace.
    """
    if cfg is None:
        return load_conf({})
    else:
        with open(cfg, 'r') as f:
            conf = yaml.load(f)
        conf_path = Path(cfg)
        hutch_dir = conf_path.parent
        return load_conf(conf, hutch_dir=hutch_dir)


def load_conf(conf, hutch_dir=None):
    """
    Step through the object loading procedure, given a configuration.

    The procedure is:

    - Check the configuration for errors
    - Display the banner by calling `hutch_banner`
    - Use ``hutch`` key to create ``hutch.db`` importable namespace to
      stash the objects. This will be literally ``hutch.db`` if hutch is
      not provided, or the hutch name e.g. ``mfx.db``.
    - Create a ``RunEngine``, ``RE``
    - Import ``plan_defaults`` and include as ``p``, ``plans``
    - Use ``hutch`` key to create a ``daq`` object with ``RE`` registered
    - Use ``db`` key to load devices from the ``happi`` beamline database
      and create a ``hutch_beampath`` object from ``lightpath``
    - Use ``load`` key to bring up the user's ``beamline`` modules
    - Use ``experiment`` key to select the current experiment

        - If ``experiment`` was missing, autoselect experiment using
          ``hutch`` key

    - Use current experiment to load experiment objects from questionnaire
    - Use current experiment to load experiment file

    If a conf key is missing, we'll note it in a ``logger.info`` message.
    If an extra conf entry is found, we'll note it in a ``logger.warning``
    message.
    If an automatically selected file is missing, we'll note it in a
    ``logger.info`` message.
    All other errors will be noted in a logger.error message.

    Parameters
    ----------
    conf: ``dict``
        ``dict`` interpretation of the original yaml file

    hutch_dir: ``Path`` or ``str``, optional
        ``Path`` object that points to the hutch's launch directory. This is
        the directory that includes the ``experiments`` directory and a
        hutchname directory e.g. ``mfx``
        If this is missing, we'll be unable to write the ``db.txt`` file,
        do relative filepath database selection for ``happi``,
        or establish a preset positions directory.

    Returns
    ------
    objs: ``dict{str: object}``
        See the return value of `load`
    """
    # Warn user about excess config entries
    for key in conf:
        if key not in VALID_KEYS:
            txt = ('Found %s in configuration, but this is not a valid key. '
                   'The valid keys are %s')
            logger.warning(txt, key, VALID_KEYS)

    # Grab configurations from dict, set defaults, show missing
    try:
        hutch = conf['hutch']
        if isinstance(hutch, str):
            hutch = hutch.lower()
        else:
            logger.error('Invalid hutch conf %s, must be string.', hutch)
            hutch = None
    except KeyError:
        hutch = None
        logger.info('Missing hutch from conf. Will skip DAQ.')
    try:
        db = conf['db']
        if isinstance(db, str):
            if db[0] == '/':
                db = Path(db)
            else:
                db = Path(hutch_dir) / db
        else:
            logger.error('Invalid db conf %s, must be string.', db)
            db = None
    except KeyError:
        db = None
        logger.info(('Missing db from conf. Will skip loading from shared '
                     'database.'))
    try:
        load = conf['load']
        if not isinstance(load, (str, list)):
            logger.error('Invalid load conf %s, must be string or list', load)
            load = None
    except KeyError:
        load = None
        logger.info('Missing load from conf. Will skip loading hutch files.')

    try:
        experiment = conf['experiment']
        if (not isinstance(experiment, dict)
                or 'proposal' not in experiment
                or 'run' not in experiment):
            logger.error(('Invalid experiment selection %s, must be a dict '
                          'with keys "proposal" and "run"'), experiment)
            experiment = None
    except KeyError:
        experiment = None
        if hutch is None:
            logger.info(('Missing hutch and experiment from conf. Will not '
                         'load objects from questionnaire or experiment '
                         'file.'))

    # Display the banner
    if hutch is None:
        hutch_banner()
    else:
        hutch_banner(hutch)

    # Make cache namespace
    cache = LoadCache((hutch or 'hutch') + '.db', hutch_dir=hutch_dir)

    # Make RunEngine
    RE = RunEngine({})
    bec = BestEffortCallback()
    RE.subscribe(bec)
    cache(RE=RE)
    try:
        install_kicker()
    except RuntimeError:
        # Probably don't have a display if this failed, so nothing to kick
        pass

    # Collect Plans
    cache(plans=plan_defaults)
    cache(p=plan_defaults)

    # Daq
    if hutch is not None:
        with safe_load('daq'):
            daq_objs = get_daq_objs(hutch, RE)
            cache(**daq_objs)

    # Happi db and Lightpath
    if db is not None:
        with safe_load('database'):
            happi_objs = get_happi_objs(db, hutch)
            cache(**happi_objs)
            bp = get_lightpath(db, hutch)
            cache(**{"{}_beampath".format(hutch.lower()): bp})

    # Load user files
    if load is not None:
        load_objs = get_user_objs(load)
        cache(**load_objs)

    # Auto select experiment if we need to
    proposal = None
    if experiment is None:
        if hutch is not None:
            try:
                # xpplp1216
                expname = get_current_experiment(hutch)
                logger.info('Selected active experiment %s', expname)
                # lp12
                proposal = expname[3:-2]
                # 16
                run = expname[-2:]
            except Exception:
                err = 'Failed to select experiment automatically'
                logger.error(err)
                logger.debug(err, exc_info=True)

    # Experiment objects
    if experiment is not None:
        proposal = experiment['proposal']
        run = experiment['run']

    if proposal is not None:
        qs_objs = get_qs_objs(proposal, run)
        cache(**qs_objs)
        user = get_exp_objs(proposal, run)
        for name, obj in qs_objs.items():
            setattr(user, name, obj)
        cache(x=user, user=user)

    # Default namespaces
    with safe_load('default groups'):
        default_class_namespace('EpicsMotor', 'motors', cache)
        default_class_namespace('Slits', 'slits', cache)
        if hutch is not None:
            meta = metadata_namespace(['beamline', 'stand'],
                                      scope='hutch_python.db')
            # Prune meta, remove branches with only one object
            for name, space in meta.__dict__.items():
                if count_ns_leaves(space) > 1:
                    cache(**{name: space})

        default_class_namespace(object, 'all_objects', cache)

    # Install Presets
    if hutch_dir is not None:
        with safe_load('position presets'):
            presets_dir = Path(hutch_dir) / 'presets'
            beamline_presets = presets_dir / 'beamline'
            preset_paths = [presets_dir, beamline_presets]
            if proposal is not None:
                experiment_presets = presets_dir / (proposal + str(run))
                preset_paths.append(experiment_presets)
            for path in preset_paths:
                if not path.exists():
                    path.mkdir()
                    path.chmod(0o777)
            if proposal is None:
                setup_preset_paths(beamline=beamline_presets)
            else:
                setup_preset_paths(beamline=beamline_presets,
                                   experiment=experiment_presets)

    # Write db.txt info file to the user's module
    try:
        cache.write_file()
    except OSError:
        logger.warning('No permissions to write db.txt file')

    return cache.objs.__dict__


def default_class_namespace(cls, name, cache):
    """
    Create a class namespace and add it to the cache.

    This is an internal utility function for `load_conf.load_conf` that creates
    an `IterableNamespace`, names it, gives it a shortened name, and then
    registers both names to the `LoadCache` if the namespace isn't empty.

    Parameters
    ----------
    cls: ``type`` or ``str``
        The class to use for the namespace

    name: ``str``
        The name of the namespace

    cache: `LoadCache`
    """
    objs = class_namespace(cls, scope='hutch_python.db')
    if len(objs) > 0:
        cache(**{name: objs, name[0]: objs})
