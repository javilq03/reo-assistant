import sys

from reo import bootstrap

if __name__ == "__main__":
    bootstrap.ensure()
    from reo.app import main
    main(debug="--debug" in sys.argv)
