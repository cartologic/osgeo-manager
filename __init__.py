import os
from .os_utils import create_direcotry
TEMP_PATH = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), 'tmp_generator')
DOWNLOADS_PATH = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), 'downloads')
create_direcotry(TEMP_PATH)
create_direcotry(DOWNLOADS_PATH)
