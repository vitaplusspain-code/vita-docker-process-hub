import event_engine


def test_package_exposes_version():
    assert event_engine.__version__ == "0.1.0"
