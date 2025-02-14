import io
import os

import requests
import torch
import torch.nn as nn
import torch.nn.functional as F
# from petrel_client.client import Client
from PIL import Image, ImageFile
from torch.nn.utils import rnn
from types import SimpleNamespace
from peft import LoraConfig, TaskType, get_peft_model
from transformers import LlamaTokenizer, LlamaForCausalLM, LlamaConfig

import numpy as np
# from header import *

from transformers import StoppingCriteria, StoppingCriteriaList

from .CLIP import load as load_clip
from .PROCESS import data
from .modeling_llama import LlamaForCausalLM
from .utils.pcl_utils import MEAN_COLOR_RGB, RandomCuboid, random_sampling
from .conversations import conversation_dict, default_conversation

ImageFile.LOAD_TRUNCATED_IMAGES = True

# sov: start of vision part; eov: end of vision part
VISION_TAGS = {
    'pos': {'image': '<image>', 'pcl': '<pcl>'},
    'sov': {'image': '<Img>', 'pcl': '<Pcl>'},
    'eov': {'image': '</Img>', 'pcl': '</Pcl>'},
}
ModalityType = SimpleNamespace(
    VISION="vision",
    TEXT="text",
    AUDIO="audio",
    THERMAL="thermal",
    DEPTH="depth",
    IMU="imu",
)

class StoppingCriteriaSub(StoppingCriteria):

    def __init__(self, stops = [], encounters=1):
        super().__init__()
        self.stops = stops
        self.ENCOUNTERS = encounters

    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor):
        stop_count = 0
        for stop in self.stops:
            stop_count = (stop == input_ids[0]).sum().item()
        if stop_count >= self.ENCOUNTERS:
            return True
        return False


class MyStoppingCriteria(StoppingCriteria):
    def __init__(self, stops, input_ids):
        super().__init__()
        self.stops = [torch.tensor(stop).to('cuda:0') for stop in stops]
        self.stop_flag = [0]*input_ids.shape[0]

    def check_stop(self, input_ids):
        for stop in self.stops:
            if torch.all((stop == input_ids[-len(stop):])).item():
                return True
        return False

    def __call__(self, output_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs) -> bool:
        flag = 1
        for id, output_id in enumerate(output_ids):
            if self.stop_flag[id] == 1:
                continue
            if self.check_stop(output_id):
                self.stop_flag[id] = 1
            else:
                flag = 0
        if flag == 1:
            return True
        return False


def build_one_instance(tokenizer, conversation, vision_type='image'):
    pos = VISION_TAGS['pos'][vision_type]
    # sov = VISION_TAGS['sov'][vision_type]
    eov = VISION_TAGS['eov'][vision_type]

    text_list = []
    turn_num = len(conversation)
    input_ids, target_ids = [], []
    for i in range(turn_num):
        turn = conversation[i]
        role = turn['from']
        if i == 0: # the first human turn
            assert role == 'human'
            turn['value'] = turn['value'].replace(f'{pos}\n', '').replace(f'\n{pos}', '')
            text = f'{eov} ' + turn['value'] + '\n### Assistant:'
            one_input_id = tokenizer(text, add_special_tokens=False).input_ids
            input_ids += one_input_id
            target_ids += [-100]*len(one_input_id) # do not perform loss regression on human prompt
        else:
            if role == 'human':
                text = 'Human: ' + turn['value'] + '\n### Assistant:'
                one_input_id = tokenizer(text, add_special_tokens=False).input_ids
                input_ids += one_input_id
                target_ids += [-100]*len(one_input_id)
            elif role == 'gpt':
                text = turn['value'] + '\n###'
                one_input_id = tokenizer(text, add_special_tokens=False).input_ids
                input_ids += one_input_id
                target_ids += one_input_id
            else:
                raise Exception('Wrong Role!!!')
        text_list.append(text)
        assert len(input_ids) == len(target_ids)
    return text_list, input_ids, target_ids


def process_batch_instance(tokenizer, batch_of_conversations, max_tgt_len, vision_type='image'):
    batch_input_ids, batch_target_ids = [], []
    for conversation in batch_of_conversations:
        _, one_input_ids, one_target_ids = build_one_instance(tokenizer, conversation, vision_type=vision_type)
        batch_input_ids.append(torch.LongTensor(one_input_ids))
        batch_target_ids.append(torch.LongTensor(one_target_ids))
    input_ids = rnn.pad_sequence(batch_input_ids, batch_first=True, padding_value=tokenizer.pad_token_id)
    target_ids = rnn.pad_sequence(batch_target_ids, batch_first=True, padding_value=-100)
    assert input_ids.size() == target_ids.size()
    input_ids = input_ids[:,:max_tgt_len]
    target_ids = target_ids[:,:max_tgt_len]
    attention_mask = input_ids.ne(tokenizer.pad_token_id)
    assert attention_mask.size() == input_ids.size()
    return input_ids, target_ids, attention_mask.long()


def make_prompt_start(system_header=False, vision_type='image', task_type='normal'):
    # TODO: choose prefix according to task type
    PROMPT_START = f'### Human: {VISION_TAGS["sov"][vision_type]}'
    if system_header:
        if task_type == 'normal':
            return f"{default_conversation.system}\n\n" + PROMPT_START
        else:
            return [f"{conversation_dict[task]}\n\n" + PROMPT_START for task in task_type]
    else:
        return PROMPT_START


class LAMMPEFTModel(nn.Module):

    '''LoRA for LLaMa model'''

    def __init__(self, **args):
        super(LAMMPEFTModel, self).__init__()
        self.args = args
        # self.client = Client('~/petreloss.conf')
        self.client = None

        self.vision_type = args['vision_type'] if 'vision_type' in args else 'image'
        encoder_pretrain = args['encoder_pretrain'] if 'encoder_pretrain' in args else 'clip'
        self.encoder_pretrain = encoder_pretrain
        assert encoder_pretrain in ['imagebind', 'clip', 'epcl'], f'Encoder_pretrain: {encoder_pretrain} Not Implemented'
        if not encoder_pretrain == 'clip' or os.path.isfile(args['encoder_ckpt_path']):
            encoder_ckpt_path = args['encoder_ckpt_path'] 
        elif not os.path.isfile(args['encoder_ckpt_path']):
            encoder_ckpt_path = 'ViT-L/14'
            
        vicuna_ckpt_path = args['vicuna_ckpt_path']
        
        system_header = args['system_header'] if 'system_header' in args else False
        stage = args['stage']

        # TODO: checkout vision token number; for ImageBind = 1; Defaultly to use 1 global token for this
        # -1 for last embedding; -2 for transformer output
        self.vision_feature_type = args['vision_feature_type']
        self.num_vision_token = args['num_vision_token']

        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        print (f'Initializing [{encoder_pretrain}] visual encoder from {encoder_ckpt_path} [{device}]...')

        # TODO: Make sure the number of vision tokens is correct
        if args['encoder_pretrain'].lower() == 'clip':
            clip_encoder, self.visual_preprocess = load_clip(encoder_ckpt_path, device=device)
            self.visual_encoder = clip_encoder.visual
            if self.vision_feature_type == 'global':          # global feature from CLIP
                self.vision_hidden_size = 768
                self.num_vision_token = 1
                assert self.num_vision_token == 1, 'Only 1 global token is available!'
            elif self.vision_feature_type == 'local':        # patch features from CLIP ViT
                self.vision_hidden_size = 1024
                self.num_vision_token = min(self.num_vision_token, 256)         # may cut partial tokens

        # freeze vision encoder
        for name, param in self.visual_encoder.named_parameters():
            param.requires_grad = False
        self.visual_encoder.eval()
        print ('Visual encoder initialized.')

        print (f'Initializing language decoder from {vicuna_ckpt_path} ...')
        # add the lora module
        peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM, 
            inference_mode=False, 
            r=self.args['lora_r'], 
            lora_alpha=self.args['lora_alpha'], 
            lora_dropout=self.args['lora_dropout'],
            target_modules=self.args['lora_target_modules']
        )

        self.llama_model = LlamaForCausalLM.from_pretrained(vicuna_ckpt_path)
        self.llama_model = get_peft_model(self.llama_model, peft_config)
        self.llama_model.print_trainable_parameters()

        self.llama_tokenizer = LlamaTokenizer.from_pretrained(vicuna_ckpt_path, use_fast=False)
        self.llama_tokenizer.pad_token = self.llama_tokenizer.eos_token
        self.llama_tokenizer.padding_side = "right"
        print ('Language decoder initialized.')

        self.llama_proj = nn.Linear(
            self.vision_hidden_size, self.llama_model.config.hidden_size
        )
        print ('LLaMa projection layer initialized.')

        self.max_tgt_len = args['max_tgt_len']
        self.system_header = system_header
        self.device = torch.cuda.current_device()

    def encode_image(self, image_paths):
        """encode images to llama inputs

        :param tupe image_paths: (bsz, )
        :return tensor, tensor: input feature to llama, attention mask to llama
        """
        if self.encoder_pretrain == 'imagebind':
            inputs = {ModalityType.VISION: data.load_and_transform_vision_data(image_paths, self.device)}
            # convert into visual dtype
            inputs = {key: inputs[key].to(self.llama_model.dtype) for key in inputs}
            with torch.no_grad():
                embeddings = self.visual_encoder(inputs)
                image_embeds = embeddings['vision']                     # bsz x 1024
            inputs_llama = self.llama_proj(image_embeds).unsqueeze(1)   # bsz x 1 x llama_size
            atts_llama = torch.ones(inputs_llama.size()[:-1], dtype=torch.long).to(self.device) # bsz x 1
            return inputs_llama, atts_llama
        elif self.encoder_pretrain == 'clip':
            inputs = self.load_and_transform_vision_data_clip(image_paths, self.device)     # bsz x 3 x 224 x 224
            inputs = inputs.to(self.llama_model.dtype)                                      # clip requires torch.float32
            inputs_llama = self.clip_encode_image(inputs)
            atts_llama = torch.ones(inputs_llama.size()[:-1], dtype=torch.long).to(self.device)                     # bsz x 1/256
            return inputs_llama, atts_llama
        
    def my_encode_image(self, images):
        """encoder loaded image objects"""
        if self.encoder_pretrain == 'clip':
            inputs = data.transform_vision_data(images, self.device)                    # bsz x 3 x 224 x 224
            inputs_llama = self.clip_encode_image(inputs)                               # bsz x 1/256 x llama_size
            atts_llama = torch.ones(inputs_llama.size()[:-1], dtype=torch.long).to(self.device)                     # bsz x 1/256
            return inputs_llama, atts_llama
        else:
            raise NotImplementedError("Encoder pretrain [{}] not implemented".format(self.encoder_pretrain))
    
    def encode_pcl(self, pcl_paths):
        # load pcl data
        inputs = self.load_and_transform_pcl_data(pcl_paths, self.device) # bsz x 40000 x 3
        
        inputs = inputs.to(self.llama_model.dtype)                                  # clip requires torch.float32
        with torch.no_grad():
            if self.vision_feature_type == 'global':
                raise NotImplementedError("Global feature not implemented for pcl")
            elif self.vision_feature_type == 'local':
                embeddings = self.visual_encoder(inputs)[1][:, :self.num_vision_token]      # bsz x 256 x 1024;
                image_embeds = embeddings.reshape(-1, self.vision_hidden_size).to(self.llama_model.dtype)       # bsz*num vision token x 1024
                inputs_llama = self.llama_proj(image_embeds).reshape(-1, self.num_vision_token, self.llama_model.config.hidden_size) # bsz x num_vision_token x llama_size
                atts_llama = torch.ones(inputs_llama.size()[:-1], dtype=torch.long).to(self.device)                     # bsz x 1/256
        return inputs_llama, atts_llama
    
    def clip_encode_image(self, inputs):
        inputs = inputs.to(self.llama_model.dtype)                                  # clip requires torch.float32
        with torch.no_grad():
            if self.vision_feature_type == 'global':
                embeddings = self.visual_encoder(inputs)                            # bsz x 768
                image_embeds = embeddings.to(self.llama_model.dtype)
                inputs_llama = self.llama_proj(image_embeds).unsqueeze(1)           # bsz x 1 x llama_size
            elif self.vision_feature_type == 'local':
                embeddings = self.visual_encoder.forward_patch_features(inputs)[:, :self.num_vision_token]      # bsz x self.num_vision_token x 1024
                image_embeds = embeddings.reshape(-1, self.vision_hidden_size).to(self.llama_model.dtype)       # bsz*num vision token x 1024
                inputs_llama = self.llama_proj(image_embeds).reshape(-1, self.num_vision_token, self.llama_model.config.hidden_size) # bsz x num_vision_token x llama_size
            else:
                raise NotImplementedError("{} not Implemented".format(self.vision_feature_type))
        return inputs_llama

    def load_and_transform_vision_data_clip(self, image_paths, device):
        if image_paths is None:
            return None
        image_ouputs = []
        for image_path in image_paths:
            if os.path.exists(image_path):
                image = Image.open(image_path)
            elif image_path.startswith('s3://') and self.client is not None:
                image = Image.open(io.BytesIO(self.client.get(image_path, update_cache=True))).convert("RGB")
            elif image_path.startswith('http://'):
                image = Image.open(requests.get(image_path, stream=True).raw)
            else:
                print("can not load image: ", image_path)
            image_outpt = self.visual_preprocess(image).to(device)                      # 3 x 224 x 224
            image_ouputs.append(image_outpt)
        return torch.stack(image_ouputs, dim=0)                                         # B x 3 x 224 x 224
    
    def load_and_transform_pcl_data(self, pcl_paths, device):
        if pcl_paths is None:
            return None
        pcl_output = []
        for pcl_path in pcl_paths:
            mesh_vertices = np.load(pcl_path)         # 150000, 3
            if not self.use_color:
                point_cloud = mesh_vertices[:, 0:3]  # do not use color for now
            else:
                point_cloud = mesh_vertices[:, 0:6]
                point_cloud[:, 3:] = (point_cloud[:, 3:] - MEAN_COLOR_RGB) / 256.0
            
            if self.use_height:
                floor_height = np.percentile(point_cloud[:, 2], 0.99)
                height = point_cloud[:, 2] - floor_height
                point_cloud = np.concatenate([point_cloud, np.expand_dims(height, 1)], 1)
            
            point_cloud, _ = random_sampling(
                point_cloud, self.num_points, return_choices=True
            )
            pcl_output.append(torch.from_numpy(point_cloud))
        return torch.stack(pcl_output, dim=0).to(device) # bsz x num_points x 3

    def prompt_wrap(self, img_embeds, input_ids, target_ids, attention_mask, system_header, task_type):
        '''
            input_ids, target_ids, attention_mask: bsz x s2
        '''
        input_ids = input_ids.to(self.device)           # bsz x s2
        target_ids = target_ids.to(self.device)         # bsz x s2
        attention_mask = attention_mask.to(self.device) # bsz x s2

        batch_size = img_embeds.shape[0]

        # return list of headers if multiple tasks
        p_before = make_prompt_start(system_header=system_header, vision_type=self.vision_type, task_type=task_type)
        if isinstance(p_before, list):
            p_before_tokens = [self.llama_tokenizer(p, 
                return_tensors="pt", add_special_tokens=False).input_ids[0].to(self.device) for p in p_before]
            # TODO: fix bug here
            p_before_token_ids = rnn.pad_sequence(p_before_tokens, batch_first=True, padding_value=self.llama_tokenizer.pad_token_id) # bsz x s1
            p_before_attn_mask = p_before_token_ids.ne(self.llama_tokenizer.pad_token_id)
        else:
            p_before_tokens = self.llama_tokenizer(p_before, 
                return_tensors="pt", add_special_tokens=False).to(self.device)  # [s1, s1...] list of batch size
            p_before_token_ids = p_before_tokens.input_ids.expand(batch_size, -1) # bsz x s1
            p_before_attn_mask = p_before_tokens.attention_mask.expand(batch_size, -1) # bsz x s1
        # peft model need deeper call
        p_before_embeds = self.llama_model.model.model.embed_tokens(p_before_token_ids) #.expand(batch_size, -1, -1) # bsz x s1 x embed_dim
        p_after_embeds = self.llama_model.model.model.embed_tokens(input_ids).expand(batch_size, -1, -1) # bsz x s2 x embed_dim
        bos = torch.ones([batch_size, 1],
                         dtype=p_before_token_ids.dtype,
                         device=p_before_token_ids.device) * self.llama_tokenizer.bos_token_id # bsz x 1
        bos_embeds = self.llama_model.model.model.embed_tokens(bos) # bsz x 1 x embed_dim
        inputs_embeds = torch.cat([bos_embeds, p_before_embeds, img_embeds, p_after_embeds], dim=1) # bsz x (1+s1+NumToken+s2) x embed_dim

        # make target ids for prefix part
        empty_targets = (
            torch.ones([batch_size, 1 + p_before_embeds.size()[1] + self.num_vision_token], # 1 (bos) + s1 + num_image_tokens (image vector)
                       dtype=torch.long).to(self.device).fill_(-100)
        ) # bsz x (1 + s1 + 1)
        targets = torch.cat([empty_targets, target_ids], dim=1) # bsz x (1 + s1 + num_image_tokens + s2)
        assert inputs_embeds.size()[1] == targets.size()[1]

        # atts_prefix = torch.ones([batch_size, 1 + p_before_embeds.size()[1] + self.num_vision_token], dtype=torch.long).to(self.device) # bsz x (1[bos] + s1 +num_image_tokens)
        atts_bos = torch.ones([batch_size, 1], dtype=torch.long).to(self.device) # bsz x 1
        atts_img = torch.ones([batch_size, self.num_vision_token], dtype=torch.long).to(self.device) # bsz x num_image_tokens
        attention_mask = torch.cat([atts_bos, p_before_attn_mask, atts_img, attention_mask], dim=1)
        assert attention_mask.size() == targets.size() # bsz x (1 + s1 + num_image_tokens + s2)
        return inputs_embeds, targets, attention_mask

    def forward(self, inputs):
        """Model Forward in training

        :param class inputs: model itself
        :raises ValueError: valueerror if not image or pcl
        :return list: loss & token acc
        """
        # image_paths = inputs['image_paths']
        assert self.vision_type == inputs['vision_type']    # single modal case
        task_type = inputs['task_type']
        vision_paths = inputs['vision_paths']
        if self.vision_type == 'image':
            vision_embeds, _ = self.encode_image(vision_paths)
        elif self.vision_type == 'pcl':
            vision_embeds, _ = self.encode_pcl(vision_paths)        # Bsz x N token x C
        else:
            raise ValueError('vision type [{}] not supported'.format(self.vision_type))

        output_texts = inputs['output_texts']
        input_ids, target_ids, attention_mask = process_batch_instance(self.llama_tokenizer, output_texts, self.max_tgt_len, self.vision_type)
        inputs_embeds, targets, attention_mask = self.prompt_wrap(vision_embeds, input_ids, target_ids, attention_mask, self.system_header, task_type)

        outputs = self.llama_model(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            return_dict=True,
            labels=targets,
        )
        loss = outputs.loss
        # calculate the token accuarcy
        chosen_tokens = torch.max(outputs.logits, dim=-1)[1][:, 1: -1]    # [B, S-1]
        labels = targets[:, 2:]
        gen_acc = (chosen_tokens.reshape(-1) == labels.reshape(-1)).to(torch.long)    # [B*S]
        valid_mask = (labels != -100).reshape(-1)
        valid_tokens = gen_acc & valid_mask    # [B*S]
        gen_acc = valid_tokens.sum().item() / valid_mask.sum().item()
        return loss, gen_acc

    def extract_multimodal_feature(self, inputs):
        """Extract multimodal features from the input in Generation (Test)

        :param Dict inputs: input dict; modality: path
        :return _type_: _description_
        """
        features = []
        if inputs['image_paths']:
            image_embeds, _ = self.encode_image(inputs['image_paths'])
            features.append(image_embeds)
        if 'images' in inputs and inputs['images']:        # image objects input in testing
            image_embeds, _ = self.my_encode_image(inputs['images'])
            return image_embeds
            # features.append(image_embeds)
        if 'pcl_paths' in inputs and inputs['pcl_paths']:
            pcl_embeds, _ = self.encode_pcl(inputs['pcl_paths'])
            features.append(pcl_embeds)
        # TODO: Cautions HERE! Multimodality allowed in test ONLY!
        feature_embeds = torch.cat(features).sum(dim=0).unsqueeze(0)        # sum all modality features together
        return feature_embeds

    def prepare_generation_embedding(self, inputs):
        """prepare for generation

        :param class inputs: model
        :return Dict: generation input
        """
        eov = VISION_TAGS['eov'][self.vision_type]
        # TODO: add System header & image token size
        prompt_list = inputs['prompt']           # questions from user
        if len(inputs['modality_embeds']) == 1:
            feature_embeds = inputs['modality_embeds'][0]
        else:
            feature_embeds = self.extract_multimodal_feature(inputs)
            inputs['modality_embeds'].append(feature_embeds)

        batch_size = feature_embeds.shape[0]
        p_before = make_prompt_start(vision_type=self.vision_type)      # no system header in test
        p_before_tokens = self.llama_tokenizer(p_before, 
            return_tensors="pt", add_special_tokens=False).to(self.device)
        p_before_embeds = self.llama_model.model.model.embed_tokens(p_before_tokens.input_ids).expand(batch_size, -1, -1) # bsz x s1 x embed_dim
        p_after_embeds_list = []
        p_after_tokens_list = []
        for prompt in prompt_list:
            # text = '</Img> ' + prompt + '\n### Assistant:'
            text = f'{eov} ' + prompt + '\n### Assistant:'
            p_after_tokens = self.llama_tokenizer(text, add_special_tokens=False, return_tensors='pt').to(self.device)

            p_after_tokens_list.append(p_after_tokens.input_ids.squeeze(0))

        p_after_tokens = rnn.pad_sequence(p_after_tokens_list, batch_first=True, padding_value=self.llama_tokenizer.pad_token_id)

        p_after_embeds = self.llama_model.model.model.embed_tokens(p_after_tokens)
        
        # text = f'{eov} ' + prompt + '\n### Assistant:'
        # p_after_tokens = self.llama_tokenizer(text, add_special_tokens=False, return_tensors='pt').to(self.device)
        # p_after_embeds = self.llama_model.model.model.embed_tokens(p_after_tokens.input_ids).expand(batch_size, -1, -1) # bsz x s1 x embed_dim
        bos = torch.ones([batch_size, 1],
                         dtype=p_before_tokens.input_ids.dtype,
                         device=p_before_tokens.input_ids.device) * self.llama_tokenizer.bos_token_id # bsz x 1
        bos_embeds = self.llama_model.model.model.embed_tokens(bos) # bsz x 1 x embed_dim
        # print(bos_embeds.shape, p_before_embeds.shape, feature_embeds.shape, p_after_embeds.shape)
        inputs_embeds = torch.cat([bos_embeds, p_before_embeds, feature_embeds, p_after_embeds], dim=1) # bsz x (1+s1+NumVisionToken+s2) x embed_dim
        return inputs_embeds

    def generate(self, inputs):
        '''
            inputs = {
                'image_paths': optional,
                'audio_paths': optional
                'video_paths': optional
                'thermal_paths': optional
                'mode': generation mode,
                'prompt': human input prompt,
                'max_tgt_len': generation length,
                'top_p': top_p,
                'temperature': temperature
                'modality_embeds': None or torch.tensor
                'modality_cache': save the image cache
            }
        '''
        input_embeds = self.prepare_generation_embedding(inputs)
        # stopping_criteria = StoppingCriteriaList([StoppingCriteriaSub(stops=[2277], encounters=1)])
        stopping_criteria = StoppingCriteriaList([MyStoppingCriteria([[2277]], input_embeds)])
        outputs = self.llama_model.generate(
            inputs_embeds=input_embeds,
            max_new_tokens=inputs['max_tgt_len'],
            top_p=inputs['top_p'],
            temperature=inputs['temperature'],
            do_sample=True,
            use_cache=True,
            stopping_criteria=stopping_criteria,
        )
        #output_text = self.llama_tokenizer.decode(outputs[0][:-2], skip_special_tokens=True)
        output_text = self.llama_tokenizer.batch_decode(outputs, skip_special_tokens=True)
        return output_text
