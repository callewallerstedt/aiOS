import unittest

from desktop_agent.loop import _scale_model_actions


class CoordinateScalingTests(unittest.TestCase):
    def test_rescales_luna_coordinates_to_capture_pixels(self):
        actions = [{"type": "click", "x": 568, "y": 974}]
        self.assertEqual(
            _scale_model_actions(actions, 1600 / 1920, 1920, 1200),
            [{"type": "click", "x": 682, "y": 1169}],
        )

    def test_rescales_drag_and_path_but_not_relative_mouse(self):
        actions = [
            {"type": "drag", "from": [100, 200], "to": [300, 400]},
            {"type": "path", "points": [[10, 20], [30, 40]]},
            {"type": "mouse_rel", "dx": 120, "dy": -30},
        ]
        result = _scale_model_actions(actions, 0.5, 1920, 1200)
        self.assertEqual(result[0]["from"], [200, 400])
        self.assertEqual(result[0]["to"], [600, 800])
        self.assertEqual(result[1]["points"], [[20, 40], [60, 80]])
        self.assertEqual(result[2], actions[2])


if __name__ == "__main__":
    unittest.main()
