from .ocr import ocr_tool
from .marks import set_of_marks_tool
from .grid import grid_tool
from .crop import crop_tool
from .color import color_mask_tool
from .icons import find_icons_tool
from .describe import describe_tool
from .sam3 import sam3_tool, sam3_available

TOOLS = {
    "ocr": ocr_tool,
    "set_of_marks": set_of_marks_tool,
    "grid": grid_tool,
    "crop": crop_tool,
    "color_mask": color_mask_tool,
    "find_icons": find_icons_tool,
    "describe": describe_tool,
    "sam3": sam3_tool,
}
