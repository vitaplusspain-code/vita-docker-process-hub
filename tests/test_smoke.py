import vitahub


def test_package_exposes_version():
    assert isinstance(vitahub.__version__, str)
    assert vitahub.__version__
