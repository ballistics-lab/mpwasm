import sys

try:
    from ._cli import main
except ImportError:  # run as a plain file (Pythonista's Run button): there is no parent package to import from
    import os

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from mpwasm._cli import main

sys.exit(main())
