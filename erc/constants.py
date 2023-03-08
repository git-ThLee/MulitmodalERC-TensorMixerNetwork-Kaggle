from collections import OrderedDict
from enum import Enum


class RunMode(Enum):
    TRAIN = "train"
    VALID = "valid"
    # Test is not used for now
    TEST = "test"


# Emotion mapper to index
emotion2idx = {
    "surprise": 0,
    "fear": 1,
    "angry": 2,
    "neutral": 3,
    "happy": 4,
    "sad": 5,
    "disgust": 6,
}

# Gender Mapper
gender2idx = {
    "M": 0,
    "F": 1,
}

# Column names to be chosen from annotations .csv
columns_kemdy19 = OrderedDict({
    "Numb": "Numb",
    "Wav": "wav_start",
    "Unnamed: 2": "wav_end",
    "ECG": "ecg_start",
    "Unnamed: 4": "ecg_end",
    "E4-EDA": "e4-eda_start",
    "Unnamed: 6": "e4-eda_end",
    "E4-TEMP": "e4-temp_start",
    "Unnamed: 8": "e4-temp_end",
    "Segment ID": "segment_id",
    "Total Evaluation": "emotion",
    "Unnamed: 11": "valence",
    "Unnamed: 12": "arousal",
})

columns_kemdy20 = OrderedDict({
    0: "Numb",
    1: "wav_start",
    2: "wav_end",
    3: "segment_id",
    4: "emotion",
    5: "valence",
    6: "arousal",
})