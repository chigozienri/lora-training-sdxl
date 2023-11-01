import hashlib
import json
import os
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import torch
from cog import BasePredictor, Input, Path
from diffusers import (
    DDIMScheduler,
    DiffusionPipeline,
    DPMSolverMultistepScheduler,
    EulerAncestralDiscreteScheduler,
    EulerDiscreteScheduler,
    HeunDiscreteScheduler,
    PNDMScheduler,
    StableDiffusionXLImg2ImgPipeline,
    StableDiffusionXLInpaintPipeline,
)
from diffusers.models.attention_processor import LoRAAttnProcessor2_0
from diffusers.pipelines.stable_diffusion.safety_checker import (
    StableDiffusionSafetyChecker,
)
from diffusers.utils import load_image
from safetensors import safe_open
from safetensors.torch import load_file
from transformers import CLIPImageProcessor

from dataset_and_utils import TokenEmbeddingsHandler
from preprocess import preprocess
from trainer_pti import main
from weights import WeightsDownloadCache

OUTPUT_DIR = "training_out"

SDXL_MODEL_CACHE = "./sdxl-cache"
REFINER_MODEL_CACHE = "./refiner-cache"
SAFETY_CACHE = "./safety-cache"
FEATURE_EXTRACTOR = "./feature-extractor"
SDXL_URL = "https://weights.replicate.delivery/default/sdxl/sdxl-vae-upcast-fix.tar"
REFINER_URL = (
    "https://weights.replicate.delivery/default/sdxl/refiner-no-vae-no-encoder-1.0.tar"
)
SAFETY_URL = "https://weights.replicate.delivery/default/sdxl/safety-1.0.tar"


class KarrasDPM:
    def from_config(config):
        return DPMSolverMultistepScheduler.from_config(config, use_karras_sigmas=True)


SCHEDULERS = {
    "DDIM": DDIMScheduler,
    "DPMSolverMultistep": DPMSolverMultistepScheduler,
    "HeunDiscrete": HeunDiscreteScheduler,
    "KarrasDPM": KarrasDPM,
    "K_EULER_ANCESTRAL": EulerAncestralDiscreteScheduler,
    "K_EULER": EulerDiscreteScheduler,
    "PNDM": PNDMScheduler,
}


def download_weights(url, dest):
    start = time.time()
    print("downloading url: ", url)
    print("downloading to: ", dest)
    subprocess.check_call(["pget", "-x", url, dest], close_fds=False)
    print("downloading took: ", time.time() - start)


class Predictor(BasePredictor):
    def predict(
        self,
        input_images: Path = Input(
            description="A .zip or .tar file containing the image files that will be used for fine-tuning"
        ),
        seed: int = Input(
            description="Random seed for reproducible training. Leave empty to use a random seed",
            default=None,
        ),
        resolution: int = Input(
            description="Square pixel resolution which your images will be resized to for training",
            default=768,
        ),
        train_batch_size: int = Input(
            description="Batch size (per device) for training",
            default=4,
        ),
        num_train_epochs: int = Input(
            description="Number of epochs to loop through your training dataset",
            default=4000,
        ),
        max_train_steps: int = Input(
            description="Number of individual training steps. Takes precedence over num_train_epochs",
            default=1000,
        ),
        # gradient_accumulation_steps: int = Input(
        #     description="Number of training steps to accumulate before a backward pass. Effective batch size = gradient_accumulation_steps * batch_size",
        #     default=1,
        # ), # todo.
        unet_learning_rate: float = Input(
            description="Learning rate for the U-Net. We recommend this value to be somewhere between `1e-6` to `1e-5`.",
            default=1e-6,
        ),
        ti_lr: float = Input(
            description="Scaling of learning rate for training textual inversion embeddings. Don't alter unless you know what you're doing.",
            default=3e-4,
        ),
        lora_lr: float = Input(
            description="Scaling of learning rate for training LoRA embeddings. Don't alter unless you know what you're doing.",
            default=1e-4,
        ),
        lora_rank: int = Input(
            description="Rank of LoRA embeddings. Don't alter unless you know what you're doing.",
            default=32,
        ),
        lr_scheduler: str = Input(
            description="Learning rate scheduler to use for training",
            default="constant",
            choices=[
                "constant",
                "linear",
            ],
        ),
        lr_warmup_steps: int = Input(
            description="Number of warmup steps for lr schedulers with warmups.",
            default=100,
        ),
        token_string: str = Input(
            description="A unique string that will be trained to refer to the concept in the input images. Can be anything, but TOK works well",
            default="TOK",
        ),
        # token_map: str = Input(
        #     description="String of token and their impact size specificing tokens used in the dataset. This will be in format of `token1:size1,token2:size2,...`.",
        #     default="TOK:2",
        # ),
        caption_prefix: str = Input(
            description="Text which will be used as prefix during automatic captioning. Must contain the `token_string`. For example, if caption text is 'a photo of TOK', automatic captioning will expand to 'a photo of TOK under a bridge', 'a photo of TOK holding a cup', etc.",
            default="a photo of TOK, ",
        ),
        mask_target_prompts: str = Input(
            description="Prompt that describes part of the image that you will find important. For example, if you are fine-tuning your pet, `photo of a dog` will be a good prompt. Prompt-based masking is used to focus the fine-tuning process on the important/salient parts of the image",
            default=None,
        ),
        crop_based_on_salience: bool = Input(
            description="If you want to crop the image to `target_size` based on the important parts of the image, set this to True. If you want to crop the image based on face detection, set this to False",
            default=True,
        ),
        use_face_detection_instead: bool = Input(
            description="If you want to use face detection instead of CLIPSeg for masking. For face applications, we recommend using this option.",
            default=False,
        ),
        clipseg_temperature: float = Input(
            description="How blurry you want the CLIPSeg mask to be. We recommend this value be something between `0.5` to `1.0`. If you want to have more sharp mask (but thus more errorful), you can decrease this value.",
            default=1.0,
        ),
        verbose: bool = Input(description="verbose output", default=True),
        checkpointing_steps: int = Input(
            description="Number of steps between saving checkpoints. Set to very very high number to disable checkpointing, because you don't need one.",
            default=999999,
        ),
        input_images_filetype: str = Input(
            description="Filetype of the input images. Can be either `zip` or `tar`. By default its `infer`, and it will be inferred from the ext of input file.",
            default="infer",
            choices=["zip", "tar", "infer"],
        ),
    ) -> Path:
        # Hard-code token_map for now. Make it configurable once we support multiple concepts or user-uploaded caption csv.
        token_map = token_string + ":2"

        # Process 'token_to_train' and 'input_data_tar_or_zip'
        inserting_list_tokens = token_map.split(",")

        token_dict = {}
        running_tok_cnt = 0
        all_token_lists = []
        for token in inserting_list_tokens:
            n_tok = int(token.split(":")[1])

            token_dict[token.split(":")[0]] = "".join(
                [f"<s{i + running_tok_cnt}>" for i in range(n_tok)]
            )
            all_token_lists.extend([f"<s{i + running_tok_cnt}>" for i in range(n_tok)])

            running_tok_cnt += n_tok

        input_dir = preprocess(
            input_images_filetype=input_images_filetype,
            input_zip_path=input_images,
            caption_text=caption_prefix,
            mask_target_prompts=mask_target_prompts,
            target_size=resolution,
            crop_based_on_salience=crop_based_on_salience,
            use_face_detection_instead=use_face_detection_instead,
            temp=clipseg_temperature,
            substitution_tokens=list(token_dict.keys()),
        )

        if not os.path.exists(SDXL_MODEL_CACHE):
            download_weights(SDXL_URL, SDXL_MODEL_CACHE)
        if os.path.exists(OUTPUT_DIR):
            shutil.rmtree(OUTPUT_DIR)
        os.makedirs(OUTPUT_DIR)

        main(
            pretrained_model_name_or_path=SDXL_MODEL_CACHE,
            instance_data_dir=os.path.join(input_dir, "captions.csv"),
            output_dir=OUTPUT_DIR,
            seed=seed,
            resolution=resolution,
            train_batch_size=train_batch_size,
            num_train_epochs=num_train_epochs,
            max_train_steps=max_train_steps,
            gradient_accumulation_steps=1,
            unet_learning_rate=unet_learning_rate,
            ti_lr=ti_lr,
            lora_lr=lora_lr,
            lr_scheduler=lr_scheduler,
            lr_warmup_steps=lr_warmup_steps,
            token_dict=token_dict,
            inserting_list_tokens=all_token_lists,
            verbose=verbose,
            checkpointing_steps=checkpointing_steps,
            scale_lr=False,
            max_grad_norm=1.0,
            allow_tf32=True,
            mixed_precision="bf16",
            device="cuda:0",
            lora_rank=lora_rank,
            is_lora=True,
        )
        return Path(f"{OUTPUT_DIR}/lora.safetensors")
