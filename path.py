import os
import sys
from pathlib import Path
import datetime

MyotionPath = os.getcwd()
ImagePath = ":/images/images/images"
IconPath = ":/icons/images/icons"
AppDataPath = ""
LogPath = ""

def initializeAppDataFolder():
    if sys.platform == "linux":
        AppDataPath = Path("/var/lib/Myotion")
        LogPath = Path("/var/log/Myotion")  
    else:
        AppDataPath = Path(os.getenv('APPDATA') + "/Myotion")
        LogPath = os.path.join(AppDataPath, "log")

    if not os.path.exists(AppDataPath):
        os.makedirs(AppDataPath)
    if not os.path.exists(LogPath):
        os.makedirs(LogPath)

    return AppDataPath, LogPath

def getSyslogFilename(LogPath):
    return os.path.join(LogPath, 'sys_'+datetime.datetime.now().strftime("%H_%M_%b_%d_%Y"))

def checkValidPath(fpath):
    return os.path.exists(fpath)
