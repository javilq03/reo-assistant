import runpy, sys
sys.argv[0] = "run.py"
runpy.run_path("run.py", run_name="__main__")
