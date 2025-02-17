import os
import argparse
import librosa
import numpy as np
from multiprocessing import Pool, cpu_count
from scipy.io import wavfile
from tqdm import tqdm
from glob import glob
import logging
from random import shuffle
from pathlib import Path
# import qvc_utils
import torch
import torchaudio
from torchaudio.functional import resample
from mel_processing import mel_spectrogram_torch
import json
import utils
from wavlm import WavLM, WavLMConfig

def encode_dataset(args):

    wav_list = []

    if(args.voice_encoder == 'softvc'):
        print(f"Loading SoftVC checkpoint")
        hmodel = torch.hub.load("bshall/hubert:main", f"hubert_soft").cuda().eval()
    elif(args.voice_encoder == 'contentvec'):
        print(f"Loading ContentVec checkpoint")
        device = "cuda" if torch.cuda.is_available() else "cpu"
        hmodel = utils.get_hubert_model().to(device)
    elif(args.voice_encoder == 'wavlm'):
        print("Loading WavLM checkpoint")
        checkpoint = torch.load('wavlm/WavLM-Large.pt')
        cfg = WavLMConfig(checkpoint['cfg'])
        hmodel = WavLM(cfg).cuda()
        hmodel.load_state_dict(checkpoint['model'])
        hmodel.eval()

        if(args.use_sr == 'y'):
            print("Loading vocoder...")
            vocoder = utils.get_vocoder(0)
            vocoder.eval()
    else:
        print(f"Voice encoder = {args.voice_encoder} not implemented!")

    print("Generating out_dir if not exist")
    if(not os.path.isdir(args.out_dir)):
        os.mkdir(args.out_dir)
    else:
        print(f"""out dir path = "{args.out_dir}" already exists, processing can generate errors!""")
    
    with open(args.config, "r") as f:
        data = f.read()
    config = json.loads(data)

    sampling_rate = config['data']['sampling_rate']
    hop_length = config['data']['hop_length']

    print(f"Processing {len(os.listdir(args.in_dir))} files at {args.in_dir}")
    for in_path in tqdm(os.listdir(args.in_dir)):
        if(args.extension in in_path):
            out_path = os.path.join(args.out_dir, in_path)
            in_path = os.path.join(args.in_dir, in_path)

            w_path = out_path 

            wav_list.append(w_path)

            wav, sr = librosa.load(in_path, sr=None)
            wav, _ = librosa.effects.trim(wav, top_db=20)
            peak = np.abs(wav).max()
            if peak > 1.0:
                wav = 0.98 * wav / peak
            wav2 = librosa.resample(wav, orig_sr=sr, target_sr=sampling_rate)
            wav2 /= max(wav2.max(), -wav2.min())
            wavfile.write(
                w_path,
                sampling_rate,
                (wav2 * np.iinfo(np.int16).max).astype(np.int16)
            )

            c_path = out_path + '.pt'
            if not os.path.exists(c_path):
                if(args.voice_encoder== 'softvc'):
                    wav_, sr = torchaudio.load(w_path)
                    wav16k = resample(wav_, sr, 16000)
                    wav16k = wav16k.cuda()

                    with torch.inference_mode():
                        units = hmodel.units(wav16k.unsqueeze(0))

                    torch.save(units.permute(0,2,1).cpu(), c_path)

                elif(args.voice_encoder== 'contentvec'):
                    wav, sr = librosa.load(w_path, sr=sampling_rate)
                    devive = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                    wav16k = librosa.resample(wav, orig_sr=sampling_rate, target_sr=16000)
                    wav16k = torch.from_numpy(wav16k).to(devive)
                    c = utils.get_hubert_content(hmodel, wav_16k_tensor=wav16k, 
                            legacy_final_proj=config['data']["contentvec_final_proj"])
                    torch.save(c.cpu(), c_path)

                elif(args.voice_encoder=='wavlm'):

                    if(args.use_sr == 'y'):
                        wav, _ = librosa.load(w_path, sr=sampling_rate)
                        wav = torch.from_numpy(wav).unsqueeze(0).cuda()
                        mel = mel_spectrogram_torch(
                            wav, 
                            config['data']['filter_length'], 
                            config['data']['n_mel_channels'], 
                            config['data']['sampling_rate'], 
                            config['data']['hop_length'], 
                            config['data']['win_length'], 
                            config['data']['mel_fmin'], 
                            config['data']['mel_fmax']
                        )

                        for i in range(args.min, args.max+1):
                            mel_rs = utils.transform(mel, i)
                            wav_rs = vocoder(mel_rs)[0][0].detach().cpu().numpy()
                            _wav_rs = librosa.resample(wav_rs, orig_sr=sampling_rate, target_sr=16000)
                            wav_rs = torch.from_numpy(_wav_rs).cuda().unsqueeze(0)
                            c = utils.get_content(hmodel, wav_rs)
                            ssl_path = c_path.replace(".pt", f"_{i}.pt")
                            torch.save(c.cpu(), ssl_path)
                            wav_path_ = w_path.replace(".wav", f"_{i}.wav")
                            wavfile.write(
                                    wav_path_,
                                    sampling_rate,
                                    _wav_rs
                            )
                    else:
                        wav, _ = librosa.load(w_path, sr=sampling_rate)
                        devive = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                        wav16k = librosa.resample(wav, orig_sr=sampling_rate, target_sr=16000)
                        wav16k = torch.from_numpy(wav16k).to(devive).unsqueeze(0)
                        c = utils.get_content(hmodel, wav16k)
                        torch.save(c.cpu(), c_path)
                else:
                    print(f"Voice encoder = {args.voice_encoder} not implemented!")

            f0_path = out_path + ".f0.npy"
            if not os.path.exists(f0_path):
                f0 = utils.compute_f0_dio(wav2, sampling_rate=sampling_rate, hop_length=hop_length)
                np.save(f0_path, f0)

            energy_path = out_path + ".energy.npy"
            if not os.path.exists(energy_path):
                energy = utils.compute_energy(wav2, sampling_rate=sampling_rate, hop_length=hop_length)
                np.save(energy_path, energy)

    print("Generating training files...")
    train = []
    val = []
    test = []
    wavs = []
    for wav_file in tqdm(wav_list):
        if(os.path.isfile(wav_file)):
            wavs.append(wav_file)

    shuffle(wavs)
    train += wavs[2:-2]
    val += wavs[:2]
    test += wavs[-2:]

    shuffle(train)
    shuffle(val)
    shuffle(test)
    
    train_list = f'./filelists/train_list_{args.model}.txt'
    val_list = f'./filelists/val_list_{args.model}.txt'
    test_list = f'./filelists/test_list_{args.model}.txt'

    print("Writing", train_list)
    with open(train_list, "w") as f:
        for fname in tqdm(train):
            wavpath = fname
            f.write(wavpath + "\n")
        
    print("Writing", val_list)
    with open(val_list, "w") as f:
        for fname in tqdm(val):
            wavpath = fname
            f.write(wavpath + "\n")
            
    print("Writing", test_list)
    with open(test_list, "w") as f:
        for fname in tqdm(test):
            wavpath = fname
            f.write(wavpath + "\n")


    print("Data preprocessing is complete, you can start training now!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Encode an audio dataset.")
    parser.add_argument(
        "--in_dir", 
        type=str, 
        default="./dataset_raw", 
        help="path to source dir")
    parser.add_argument(
        "--out_dir", 
        type=str, 
        default="./dataset", 
        help="path to target dir")
    parser.add_argument(
        "--extension",
        help="extension of the audio files (defaults to .flac).",
        default=".wav",
        type=str,
    )
    parser.add_argument(
        '-m', 
        '--model', 
        type=str, 
        required=True,
        help='Model name'
    )
    parser.add_argument(
        '-c',
        '--config',
        type=str,
        default="./configs/quickvc.json",
        help='JSON file for configuration')
    parser.add_argument(
        '-venc',
        '--voice_encoder',
        type=str,
        default="softvc",
        help='Model to extract content representations')
    parser.add_argument(
        '-sr',
        '--use_sr',
        type=str,
        default="y",
        help='whether use or not spectrogram augmentation')
    parser.add_argument("--min", type=int, default=68, help="min")
    parser.add_argument("--max", type=int, default=92, help="max")

    args = parser.parse_args()
    encode_dataset(args)