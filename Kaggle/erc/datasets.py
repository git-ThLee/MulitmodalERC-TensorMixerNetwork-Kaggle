import os
import random
from typing import List

from pathlib import Path
from typing import Tuple
from glob import glob

import hydra
import datasets
import omegaconf
import numpy as np
import pandas as pd
import torch
from torch.utils.data import Dataset
import torchaudio
from transformers import AutoProcessor, AutoTokenizer

from erc.preprocess import get_folds, merge_csv_kemdy19, merge_csv_kemdy20
from erc.utils import check_exists, get_logger
from erc.constants import (
    RunMode,
    emotion2idx,
    gender2idx,
    emotion_va_19_dict,
    emotion_va_20_dict,
)


random.seed(42)

logger = get_logger(name=__name__)


class KEMDBase(Dataset):
    """ Abstract class base for KEMD dataset """
    NUM_FOLDS = 5
    def __init__(
        self,
        base_path: str,
        generate_csv: bool = False,
        return_bio: bool = False,
        max_length_wav: int = 200_000,
        max_length_txt: int = 50,
        tokenizer_name: str = "klue/roberta-large",
        multilabel: bool = False,
        remove_deuce: bool = False,
        validation_fold: int = 4,
        mode: RunMode | str = RunMode.TRAIN,
        num_data: int = None,
        **kwargs
    ):
        """
        Args:
            base_path:
                Only used when csv is not found. Optional
            generate_csv:
                Flag to generate a new label.csv, default=False
            return_bio:
                Flag to call and return full ECG / EDA / TEMP data
                Since csv in annotation directory contains start/end value of above signals,
                this is not necessary. default=False
            validation_fold:
                Indicates validation fold.
                Fold split is based on Session number
                i.e. 
                    - Fold 0: Session 1 - 4
                    - Fold 1: Session 5 - 8
                    - Fold 2: Session 9 - 12
                    - Fold 3: Session 13 - 16
                    - Fold 4: Session 17 - 20
                If -1 given, load the whole fold
            mode:
                Train / valid / test mode.
        """
        logger.debug("Instantiate %s Dataset", self.NAME)
        self.base_path: Path = Path(base_path)
        self.return_bio = return_bio
        self.max_length_wav = max_length_wav
        self.max_length_txt = max_length_txt
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_name) if tokenizer_name else None
        self.multilabel = multilabel
        self.remove_deuce = remove_deuce
        emo_col = list(emotion2idx.keys())
        emo_col.remove("disqust")
        self.emo_col = emo_col if multilabel else "emotion"
        # This assertion is subject to change: number of folds to split
        assert isinstance(validation_fold, int) and validation_fold in range(-1, 5),\
            f"Validation fold should lie between 0 - 4, int. Given: {validation_fold}"
        self.validation_fold = validation_fold
        self.mode = RunMode[mode.upper()] if isinstance(mode, str) else mode
        self.df: pd.DataFrame = self.processed_db(generate_csv=generate_csv,
                                                  fold_num=validation_fold)
        
        # Limit number of data for debug (Fast Dev)
        if isinstance(num_data, int):
            if num_data in range(0, len(self.df)):
                self.num_data = num_data
            else:
                self.num_data = round(0.05 * len(self.df))
        else:
            self.num_data = None

    def __len__(self):
        return len(self.df) if not self.num_data else len(self.df[:self.num_data])

    def __getitem__(self, idx: int):
        row = self.df.iloc[idx]
        segment_id = row["segment_id"]
        data = {"segment_id": segment_id}
        _, _, gender, wav_prefix = self.parse_segment_id(segment_id=segment_id)
        
        wav_path = wav_prefix / f"{segment_id}.wav"
        txt_path = wav_prefix / f"{segment_id}.txt"
        if not os.path.exists(wav_path) or not os.path.exists(txt_path):
            # Pre-checking data existence
            # TODO This should be dealed! Not a good behavior
            logger.warn("Error occurs -> %s", wav_prefix)
            return data

        # Wave File
        sampling_rate, wav, wav_mask = self.get_wav(wav_path=wav_path)
        data["sampling_rate"] = sampling_rate
        data["wav"] = wav
        data["wav_mask"] = wav_mask
        
        # Txt File
        txt, txt_mask = self.get_txt(txt_path=txt_path, encoding=self.TEXT_ENCODING)
        data["txt"] = txt
        data["txt_mask"] = txt_mask
        
        # Bio Signals
        # Currently returns average signals across time elapse
        if self.return_bio:
            for bio in ["ecg", "e4-eda", "e4-temp"]:
                s, e = map(float, row[[f"{bio}_start", f"{bio}_end"]])
                data[bio] = torch.tensor((s + e) / 2, dtype=torch.float)
                
        # Emotion
        data["emotion"] = self.get_emo(row[self.emo_col])

        # Valence & Arousal
        valence, arousal = map(float, row[["valence", "arousal"]])
        data["valence"] = torch.tensor(valence, dtype=torch.float)
        data["arousal"] = torch.tensor(arousal, dtype=torch.float)

        # Vote Emotion
        if self.multilabel:
            data["vote_emotion"] = self.get_hard_vote(data=data)

        # Man-Female
        data["gender"] = self.gender2num(gender) # Sess01_script01_F003
        return data
    
    def pad_value(
        self,
        arr: torch.Tensor,
        max_length: int,
        pad_value: int | float = 0,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """ Assumes single data """
        if not isinstance(arr, torch.Tensor):
            arr = torch.tensor(arr)
        
        mask = torch.ones(max_length).long()
        if len(arr) >= max_length:
            arr = arr[:max_length]
        else:
            null_size = max_length - len(arr)
            arr = torch.nn.functional.pad(arr, pad=(0, null_size), value=pad_value)
            mask[null_size:] = 0
        return arr, mask

    def get_wav(self, wav_path: Path | str) -> torch.Tensor | np.ndarray:
        """ Get output feature vector from pre-trained wav2vec model
        XXX: Embedding outside dataset, to fine-tune pre-trained model? See Issue
        """
        wav_path = check_exists(wav_path)
        data, sampling_rate = torchaudio.load(wav_path)
        if self.max_length_wav:
            # If self.max_length_wav is given, return a padded value
            # Else, just return naive wav file.
            data, mask = self.pad_value(data.squeeze(), max_length=self.max_length_wav)
        else:
            data = data.squeeze()
            mask = None
        return sampling_rate, data, mask

    def get_txt(self, txt_path: Path | str, encoding: str = None) -> Tuple[torch.Tensor, torch.Tensor]:
        """ Get output feature vector from pre-trained txt model
        :parameters:
            txt_path: Path to text in Sessions
            encoding: For KEMDy19, None. For KEMDy20_v1_1, cp949
        TODO:
            1. How to process special cases: /o /l 
                -> They _maybe_ processed by pre-trained toeknizers
            2. Which model to use
        """
        txt_path = check_exists(txt_path)
        with open(txt_path, mode="r", encoding=encoding) as f:
            txt: list = f.readlines()
        # We assume there is a single line
        txt: str = " ".join(txt)
        if self.tokenizer:
            result: dict = self.tokenizer(text=txt,
                                        padding="max_length",
                                        truncation="only_first",
                                        max_length=self.max_length_txt,
                                        return_attention_mask=True,
                                        return_tensors="pt")
            input_ids = result["input_ids"].squeeze()
            mask = result["attention_mask"].squeeze()
            return input_ids, mask
        else:
            return txt, None
        
    def get_hard_vote(self, data: dict) -> torch.Tensor:
        e = data["emotion"]
        v = data["valence"]
        a = data["arousal"]
        
        m = e == e.max()
        if len(e[m]) > 1:
            regress = torch.stack([v, a]).numpy()
            ve = self._find_deuce_label(regress=regress, mask=m)
        else:
            ve = torch.tensor(e.argmax())
        return ve
    
    def _find_deuce_label(self, regress: np.ndarray, mask: np.ndarray) -> torch.Tensor:
        '''
        Summary:
            동률인 것 중 거리가 가장 작은 것을 사용하여 라벨을 확정한다. 
        Input
        regress = np.array([valence, arousal])
        deuce_mask = np.array([False,  True, False,  True, False, False, False])
        Return
            voted index 
        '''
        mask = mask.nonzero()[0]
        total_dist = []
        eva_dict = {
            "kemdy19": emotion_va_19_dict,
            "kemdy20": emotion_va_20_dict,
        }[self.NAME.lower()]
        for index in mask:
            center = np.array(eva_dict[f'{index}_centroid'])
            dist = np.linalg.norm(regress - center, ord=2)
            total_dist.append(dist)
        return torch.tensor(mask[np.array(total_dist).argmin()]) # select minumum value index

    def processed_db(self, generate_csv: bool = False, fold_num: int = 4) -> pd.DataFrame:
        """ Reads in .csv file if exists.
        If pre-processed .csv file does NOT exists, read from data path. """
        if not os.path.exists(self.TOTAL_DF_PATH) or generate_csv:
            logger.info(f"{self.TOTAL_DF_PATH} does not exists. Process from raw data")
            total_df = self.merge_csv(base_path=self.base_path,
                                      save_path=self.TOTAL_DF_PATH,
                                      exclude_multilabel=not self.multilabel)
        else:
            try:
                total_df = pd.read_csv(self.TOTAL_DF_PATH)
                if self.multilabel:
                    # Check if multilabel data has seven extra columns
                    if not set(self.emo_col) & set(total_df.columns):
                        total_df = self.merge_csv(base_path=self.base_path,
                                                  save_path=self.TOTAL_DF_PATH,
                                                  exclude_multilabel=False)
            except pd.errors.EmptyDataError as e:
                logger.error(f"{self.TOTAL_DF_PATH} seems to be empty")
                logger.exception(e)
                total_df = None
        
        df = self.split_folds(total_df=total_df, fold_num=fold_num, mode=self.mode)
        if self.remove_deuce:
            df = df[~df['emotion'].str.contains(';')]
        return df
    
    def split_folds(
        self,
        total_df: pd.DataFrame,
        fold_num: int,
        mode: RunMode | str = RunMode.TRAIN,
    ) -> pd.DataFrame:
        if fold_num == -1:
            return total_df
        else:
            sessions: pd.Series = total_df["segment_id"].apply(lambda s: s.split("_")[0][-2:])
            sessions = sessions.apply(int)
            fold_dict: dict = get_folds(num_session=self.NUM_SESSIONS, num_folds=self.NUM_FOLDS)
            fold_range: range = fold_dict[fold_num]
            loc = ~sessions.isin(fold_range) if mode == RunMode.TRAIN else sessions.isin(fold_range)
            return total_df.loc[loc]
        
    def get_emo(self, emotion: str | pd.Series) -> int | np.ndarray:
        if isinstance(emotion, str):
            # Single label cases
            return self.str2num(emotion)
        else:
            # Multilabel label cases
            return self.vectorize(emotion)

    def vectorize(self, emotion: pd.Series) -> np.ndarray:
        ev = emotion.values
        ev = ev / ev.sum()
        return ev
    
    def str2num(self, key: str) -> torch.Tensor:
        emotion = emotion2idx.get(key, -1)
        return torch.tensor(emotion, dtype=torch.long)
    
    def gender2num(self, key: str) -> torch.Tensor:
        gender = gender2idx.get(key, -1)
        return torch.tensor(gender, dtype=torch.long)
    
    def parse_segment_id(self, segment_id: str) -> Tuple[str, str, str, str]:
        """ Parse `segment_id` into useful information 
        This varies across dataset. Needs to be implemented for `__getitem__` method """
        raise NotImplementedError

    def merge_csv(
        self,
        base_path: str | Path = "/kaggle/input/kemdy-dataset/KEMDy20_v1_1",
        save_path: str | Path = "/kaggle/input/kemdy-dataset/kemdy20.csv",
        exclude_multilabel: bool = True,
    ):
        """ Loads all annotation .csv and merge into a single csv.
        This function is called when target .csv is not found. """
        raise NotImplementedError
    

class KEMDy19Dataset(KEMDBase):
    NAME = "KEMDy19"
    WAV_PATH_FMT = "/kaggle/input/kemdy-dataset/KEMDy19/wav/Session{0}/Sess{0}_{1}"
    EDA_PATH_FMT = "/kaggle/input/kemdy-dataset/KEMDy19/EDA/Session{0}/Original/Sess{0}{1}.csv"

    NUM_SESSIONS = 20
    TOTAL_DF_PATH = "/kaggle/input/kemdy-dataset/kemdy19.csv"
    TEXT_ENCODING: str = None

    def __init__(
        self,
        base_path: str = "/kaggle/input/kemdy-dataset/KEMDy19",
        generate_csv: bool = False,
        return_bio: bool = True,
        max_length_wav: int = 200_000,
        max_length_txt: int = 50,
        tokenizer_name: str = None,
        multilabel: bool = False,
        remove_deuce: bool = True,
        validation_fold: int = 4,
        mode: RunMode | str = RunMode.TRAIN,
        num_data: int = None,
        **kwargs
    ):
        super(KEMDy19Dataset, self).__init__(
            base_path,
            generate_csv,
            return_bio,
            max_length_wav,
            max_length_txt,
            tokenizer_name,
            multilabel,
            remove_deuce,
            validation_fold,
            mode,
            num_data,
        )

    def merge_csv(
        self,
        base_path: str | Path = "/kaggle/input/kemdy-dataset/KEMDy19",
        save_path: str | Path = "/kaggle/input/kemdy-dataset/kemdy19.csv",
        exclude_multilabel: bool = True,
    ):
        return merge_csv_kemdy19(base_path=base_path,
                                 save_path=save_path,
                                 exclude_multilabel=exclude_multilabel)

    def parse_segment_id(self, segment_id: str) -> Tuple[str, str, str, str]:
        """ KEMDy19
            segment_id: Sess01_script01_M001
            prefix: 'KEMDy19/wav/Session01/Sess01_impro01'
        """
        session, script_type, speaker = segment_id.split("_")
        wav_prefix = Path(self.WAV_PATH_FMT.format(session[-2:], script_type))
        gender = speaker[0]
        return session, speaker, gender, wav_prefix


class KEMDy20Dataset(KEMDBase):
    NAME = "KEMDy20"
    WAV_PATH_FMT = "/kaggle/input/kemdy-dataset/KEMDy20_v1_1/wav/Session{0}"
    # Not used yet
    EDA_PATH_FMT = "/kaggle/input/kemdy-dataset/KEMDy20v_1_1/EDA/Session{0}/Original/Sess{0}{1}.csv"

    NUM_SESSIONS = 40
    TOTAL_DF_PATH = "/kaggle/input/kemdy-dataset/kemdy20.csv"
    TEXT_ENCODING: str = "cp949"

    def __init__(
        self,
        base_path: str = "/kaggle/input/kemdy-dataset/KEMDy20_v1_1",
        generate_csv: bool = False,
        return_bio: bool = False,
        max_length_wav: int = 200_000,
        max_length_txt: int = 50,
        tokenizer_name: str = None,
        multilabel: bool = False,
        remove_deuce: bool = True,
        validation_fold: int = 4,
        mode: RunMode | str = RunMode.TRAIN,
        num_data: int = None,
        **kwargs
    ):
        super(KEMDy20Dataset, self).__init__(
            base_path,
            generate_csv,
            return_bio,
            max_length_wav,
            max_length_txt,
            tokenizer_name,
            multilabel,
            remove_deuce,
            validation_fold,
            mode,
            num_data,
        )

    def merge_csv(
        self,
        base_path: str | Path = "/kaggle/input/kemdy-dataset/KEMDy20_v1_1",
        save_path: str | Path = "/kaggle/input/kemdy-dataset/kemdy20.csv",
        exclude_multilabel: bool = True,
    ):
        return merge_csv_kemdy20(base_path=base_path,
                                 save_path=save_path,
                                 exclude_multilabel=exclude_multilabel)

    def parse_segment_id(self, segment_id: str) -> Tuple[str, str, str, str]:
        """
        KEMDy20_v1_1
            segment_id: Sess01_script01_User002M_001
            prefix: 'KEMDy20_v1_1/wav/Session01'
        """
        session, _, speaker, _ = segment_id.split("_")
        wav_prefix = Path(self.WAV_PATH_FMT.format(session[-2:]))
        gender = speaker[-1]
        return session, speaker, gender, wav_prefix


class HF_KEMD:
    def __init__(
        self,
        paths: str = "kemdy19-kemdy20",
        validation_fold: int = 4,
        save_to_disk: bool = True,
        PRETRAINED_DATA_PATH: str = "./aihub",
        mode: RunMode | str = RunMode.TRAIN,
        wav_processor: str = "kresnik/wav2vec2-large-xlsr-korean",
        sampling_rate: int = 16_000,
        wav_max_length: int = 112_000, # 16_000 * 7, 7secs duration
        txt_processor: str = "klue/roberta-large",
        txt_max_length: int = 64,
        multilabel: bool = False,
        remove_deuce: bool = True,
        load_from_cache_file: bool = True,
        num_proc: int = 1,
        batched: bool = True,
        batch_size: int = 1000, # Not a torch batch_size
        writer_batch_size: int = 1000,
        num_data: int = None,
        preprocess: bool = True,
    ):
        """ Loads dataset and do pre-process
        Trunctae wav & text to designated maximum length
        Save to cache directory.
        Load from cache when cache_file_name is available.
        This is required since a full run takes around 20 minutes with no `num_proc`
        With `num_proc=8`, the whole process takes around 3 minutes.
        TODO: Check with DDP / Accelerate
        """
        # This assertion is subject to change: number of folds to split
        assert isinstance(validation_fold, int) and validation_fold in range(-1, 5),\
            f"Validation fold should lie between 0 - 4, int. Given: {validation_fold}"
        self.validation_fold = validation_fold
        logger.info("Load %s Huggingface KEMD Dataset", mode)
        self.mode = RunMode[mode.upper()] if isinstance(mode, str) else mode

        ds_name = f"{paths}_{self.mode.value}{validation_fold}_multilabel{multilabel}_rdeuce{remove_deuce}" # remove deuce 
        try:
            logger.info("Try Loading dataset %s from disk", ds_name)
            print(ds_name)
            self.ds = datasets.load_from_disk(ds_name)
            if mode == 'train':
              self.ds = self.ds.select(list(range(num_data)))
            print(self.ds)
            logger.info("Successfully loaded %s from disk", ds_name)
            logger.info("# Datapoints %s", len(self))

        except FileNotFoundError:
            if os.path.exists(ds_name):
                logger.warn("Was not able to load %s. Please check dataset path", ds_name)
            else:
                logger.info("File not found. Generate hf dataset from scratch")
                logger.info(
                    "Note that if you're running `train.py`, num_proc should be 1, due to unknown deadlocks"
                )
                logger.info("num_proc given: %s", num_proc)
                
            ds_kwargs = dict(
                # Note for hard-coded kwargs
                generate_csv=False,
                return_bio=False,
                tokenizer_name=None, # Tokenization after loading torch dataset
                max_length_wav=wav_max_length,
                max_length_txt=txt_max_length,
                multilabel=multilabel,
                remove_deuce=remove_deuce,
                validation_fold=validation_fold,
                mode=mode,
                num_data=num_data,
                PRETRAINED_DATA_PATH=PRETRAINED_DATA_PATH,
            )
            ds: torch.utils.data.Dataset = self.load_dataset(paths=paths, **ds_kwargs)
            def gen():
                for idx in range(len(ds)):
                    yield ds[idx]
            # TODO: multiprocessing requires extra shards in `from_generator`
            # https://huggingface.co/docs/datasets/v2.10.0/en/package_reference/main_classes#datasets.Dataset
            self.ds: datasets.arrow_dataset.Dataset = datasets.Dataset.from_generator(gen)

            # Wave Process
            logger.info("Load wave processor from %s", wav_processor)
            self.wav_processor = AutoProcessor.from_pretrained(wav_processor) if wav_processor else None
            self.wav_kwargs = dict(
                sampling_rate=sampling_rate,
                max_length=wav_max_length,
                truncation="only_first",
                padding="max_length",
                padding_value=0,
                return_attention_mask=True,
                return_tensors="pt",
            )

            # Text Tokenizer
            logger.info("Load text processor from %s", txt_processor)
            self.txt_processor = AutoTokenizer.from_pretrained(txt_processor) if txt_processor else None
            self.txt_kwargs = dict(
                max_length=txt_max_length,
                truncation="only_first",
                padding="max_length",
                return_attention_mask=True,
                return_tensors="pt",
            )

            # Pre-process
            self.map_kwargs = dict(
                batched=batched,
                batch_size=batch_size,
                desc=f"Pre-process wave & text {mode}",
                num_proc=num_proc,
            )
            if preprocess:
                logger.info("Start pre-processing")
                self.ds = self.ds.map(self.preprocess, **self.map_kwargs).with_format("torch")
                logger.info("End up pre-processing")
            if save_to_disk:
                self.ds.save_to_disk(ds_name)
                logger.info("Sucessfully saved to disk as %s", ds_name)

    def __len__(self):
        return len(self.ds)

    def __getitem__(self, idx: int):
        return self.ds[idx]

    def preprocess(self, batch: list):
        """ Mapping function for hf dataset """
        wav = self.wav_processor(audio=batch["wav"], **self.wav_kwargs)
        batch["wav"] = wav["input_values"]
        batch["wav_mask"] = wav["attention_mask"]

        txt = self.txt_processor(text=batch["txt"], **self.txt_kwargs)
        batch["txt"] = txt["input_ids"]
        batch["txt_mask"] = txt["attention_mask"]
        return batch

    def _load_dataset(self, path: str | Path, **kwargs):
        ds: torch.utils.data.Dataset = {
            "kemdy19": KEMDy19Dataset,
            "kemdy20": KEMDy20Dataset,
            "aihub": AIHubDialog,
        }[path](**kwargs)
        logger.info("%s dataset has %s data", path, len(ds))
        logger.info("Sample data %s", ds[0])
        return ds

    def load_dataset(self, paths: List[str | Path], **kwargs):
        try:
            paths = paths.split("-")
            logger.info("Loading PyTorch dataset.")
            logger.info("config: %s", kwargs)
            ds = torch.utils.data.ConcatDataset(
                [self._load_dataset(path, **kwargs) for path in paths]
            )
        except:
            logger.warn("Wrongly given dataset. %s", paths)
            raise
        return ds


class AIHubDialog(KEMDBase):
    '''
    With this class, we can split just one-fold of subset in AI-Hub dataset.
    Also, there are no arousal, valence score in the ai-hub dataset i.e. only can interprete txt, wav 
    '''
    NAME = "AIHubDialog"
    def __init__(self, 
                 PRETRAINED_DATA_PATH: str | Path,
                 mode: str,
                 **kwargs):
        self.txt_folder_total = sorted(glob(os.path.join(PRETRAINED_DATA_PATH, 'annotation')+'/*.csv'))
        self.wav_folder_total = sorted(glob(os.path.join(PRETRAINED_DATA_PATH, 'wav')+'/*.wav'))
        self.max_length_wav = None

        # split into train-valid 
        index_list = self.sampling_with_ratio(len(self.txt_folder_total), mode, train_ratio=0.8)
        self.txt_folder = self.get_sub_list(self.txt_folder_total, index_list)
        self.wav_folder = self.get_sub_list(self.wav_folder_total, index_list)
        
    def __len__(self):
        assert len(self.wav_folder) == len(self.txt_folder)
        return len(self.wav_folder) 
    
    def __getitem__(self, idx: int):
        data = {}
        txt, segment_id, emotion = pd.read_csv(self.txt_folder[idx]).iloc[0].values
        data['segment_id'] = segment_id

        # Txt File
        data["txt"] = txt
        data["txt_mask"] = None

        # emotion 
        data["emotion"] = self.get_emo(emotion)

        sampling_rate, wav, wav_mask = self.get_wav(wav_path=self.wav_folder[idx])
        data["sampling_rate"] = sampling_rate
        data["wav"] = wav
        data["wav_mask"] = wav_mask

        # Arousal / Valence
        data["arousal"] = 0
        data["valence"] = 0

        return data

    @staticmethod
    def sampling_with_ratio(total_len: int, mode: str, train_ratio = 0.8) -> list[int]:
        '''
            Using Numpy sample, we split the train valid with train_ratio
        '''
        total_idx = [i for i in range(total_len)]
        train_num = int(total_len * train_ratio)

        train_idx = random.sample(total_idx, train_num)
        valid_idx = list(set(total_idx) - set(train_idx))

        if mode == "train":
            index_list = train_idx
        elif mode == "valid":
            index_list = valid_idx
        else:
            assert "check mode"

        return index_list

    @staticmethod
    def get_sub_list(in_list, in_indices):
        """리스트에서 복수인덱스 값을 가져온다"""
        return [in_list[i] for i in in_indices]


def get_dataloaders(ds_cfg: omegaconf.DictConfig,
                    dl_cfg: omegaconf.DictConfig,
                    modes: list = ["train", "valid"]) -> dict:
    dl_dict = {"train": None, "valid": None, "test": None}
    for mode in modes:
        # Should load saved datasets
        # Preprocess from scratch has errors
        # 1. `num_proc` > 1 gets deadlocked
        # 2. `num_proc` = 1 will take 20 minutes for pre-processing
        _ds = hydra.utils.instantiate(ds_cfg, mode=mode).ds
        _dl = hydra.utils.instantiate(dl_cfg,
                                      dataset=_ds,
                                      shuffle=(True if mode == "train" else False))
        dl_dict[mode] = _dl
    return dl_dict


if __name__=="__main__":
    dataset = KEMDy20Dataset(base_path="/kaggle/input/kemdy-dataset/KEMDy20_v1_1",
                             generate_csv=True,
                             return_bio=False,
                             validation_fold=4,
                             mode="train")
    print(dataset[0])
