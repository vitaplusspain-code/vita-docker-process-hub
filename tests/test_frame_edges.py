from vitahub.analytics.frame_edges import touches_bottom, touches_edge

FRAME = (640, 360)


def test_touches_edge_left_side():
    assert touches_edge((0, 50, 100, 200), FRAME)


def test_touches_edge_bottom_within_margin():
    assert touches_edge((10, 50, 100, 359), FRAME)


def test_touches_edge_false_inside():
    assert not touches_edge((10, 50, 100, 300), FRAME)


def test_touches_edge_right_and_top():
    assert touches_edge((10, 50, 639, 300), FRAME)
    assert touches_edge((10, 1, 100, 300), FRAME)


def test_touches_bottom_false_for_top_only():
    assert not touches_bottom((10, 0, 100, 200), FRAME)
    assert touches_bottom((10, 0, 100, 358), FRAME)


def test_none_frame_size_never_touches():
    assert not touches_edge((0, 0, 640, 360), None)
    assert not touches_bottom((0, 0, 640, 360), None)
