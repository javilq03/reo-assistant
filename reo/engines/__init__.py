def server_exe():
    """Path of the installed llama-server, or None."""
    from ..server_install import exe_path
    return exe_path()
