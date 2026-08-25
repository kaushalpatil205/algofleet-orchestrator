def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: loads a real strategy end to end; mutates sys.modules and cwd")
