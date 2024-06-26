# SPDX-FileCopyrightText: Copyright (c) 2022-2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import Optional

from ...functional import cast, concat, silu
from ...layers import Conv2d, GroupNorm
from ...module import Module, ModuleList
from .embeddings import TimestepEmbedding, Timesteps
from .unet_2d_blocks import (UNetMidBlock2DCrossAttn, get_down_block,
                             get_up_block)


class UNet2DConditionModel(Module):

    def __init__(
        self,
        sample_size=None,
        in_channels=4,
        out_channels=4,
        center_input_sample=False,
        flip_sin_to_cos=True,
        freq_shift=0,
        down_block_types=("CrossAttnDownBlock2D", "CrossAttnDownBlock2D",
                          "CrossAttnDownBlock2D", "DownBlock2D"),
        up_block_types=("UpBlock2D", "CrossAttnUpBlock2D", "CrossAttnUpBlock2D",
                        "CrossAttnUpBlock2D"),
        block_out_channels=(320, 640, 1280, 1280),
        layers_per_block=2,
        downsample_padding=1,
        mid_block_scale_factor=1.0,
        act_fn="silu",
        norm_num_groups=32,
        norm_eps=1e-5,
        cross_attention_dim=1280,
        transformer_layers_per_block=1,
        attention_head_dim=8,
        use_linear_projection=False,
        addition_embed_type: Optional[str] = None,
        addition_time_embed_dim: Optional[int] = None,
        projection_class_embeddings_input_dim: Optional[int] = None,
        dtype=None,
    ):
        super().__init__()

        self.sample_size = sample_size
        self.addition_embed_type = addition_embed_type
        time_embed_dim = block_out_channels[0] * 4

        # input
        self.conv_in = Conv2d(in_channels,
                              block_out_channels[0],
                              kernel_size=(3, 3),
                              padding=(1, 1),
                              dtype=dtype)
        # time
        self.time_proj = Timesteps(block_out_channels[0],
                                   flip_sin_to_cos,
                                   freq_shift,
                                   dtype=dtype)
        timestep_input_dim = block_out_channels[0]

        self.time_embedding = TimestepEmbedding(timestep_input_dim,
                                                time_embed_dim,
                                                dtype=dtype)

        if addition_embed_type == "text_time":
            self.add_time_proj = Timesteps(addition_time_embed_dim,
                                           flip_sin_to_cos,
                                           freq_shift,
                                           dtype=dtype)
            self.add_embedding = TimestepEmbedding(
                projection_class_embeddings_input_dim,
                time_embed_dim,
                dtype=dtype)

        down_blocks = []
        up_blocks = []

        if isinstance(attention_head_dim, int):
            attention_head_dim = (attention_head_dim, ) * len(down_block_types)

        if isinstance(transformer_layers_per_block, int):
            transformer_layers_per_block = [transformer_layers_per_block
                                            ] * len(down_block_types)

        # down
        output_channel = block_out_channels[0]
        for i, down_block_type in enumerate(down_block_types):
            input_channel = output_channel
            output_channel = block_out_channels[i]
            is_final_block = i == len(block_out_channels) - 1

            down_block = get_down_block(
                down_block_type,
                num_layers=layers_per_block,
                transformer_layers_per_block=transformer_layers_per_block[i],
                in_channels=input_channel,
                out_channels=output_channel,
                temb_channels=time_embed_dim,
                add_downsample=not is_final_block,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                cross_attention_dim=cross_attention_dim,
                attn_num_head_channels=attention_head_dim[i],
                downsample_padding=downsample_padding,
                use_linear_projection=use_linear_projection,
                dtype=dtype)
            down_blocks.append(down_block)
        self.down_blocks = ModuleList(down_blocks)
        # mid
        self.mid_block = UNetMidBlock2DCrossAttn(
            in_channels=block_out_channels[-1],
            temb_channels=time_embed_dim,
            resnet_eps=norm_eps,
            resnet_act_fn=act_fn,
            output_scale_factor=mid_block_scale_factor,
            transformer_layers_per_block=transformer_layers_per_block[-1],
            resnet_time_scale_shift="default",
            cross_attention_dim=cross_attention_dim,
            attn_num_head_channels=attention_head_dim[-1],
            resnet_groups=norm_num_groups,
            use_linear_projection=use_linear_projection,
            dtype=dtype,
        )
        # up
        reversed_block_out_channels = list(reversed(block_out_channels))
        reversed_attention_head_dim = list(reversed(attention_head_dim))
        reversed_transformer_layers_per_block = list(
            reversed(transformer_layers_per_block))
        output_channel = reversed_block_out_channels[0]
        for i, up_block_type in enumerate(up_block_types):
            prev_output_channel = output_channel
            output_channel = reversed_block_out_channels[i]
            input_channel = reversed_block_out_channels[min(
                i + 1,
                len(block_out_channels) - 1)]

            is_final_block = i == len(block_out_channels) - 1

            up_block = get_up_block(
                up_block_type,
                num_layers=layers_per_block + 1,
                transformer_layers_per_block=
                reversed_transformer_layers_per_block[i],
                in_channels=input_channel,
                out_channels=output_channel,
                prev_output_channel=prev_output_channel,
                temb_channels=time_embed_dim,
                add_upsample=not is_final_block,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                cross_attention_dim=cross_attention_dim,
                attn_num_head_channels=reversed_attention_head_dim[i],
                use_linear_projection=use_linear_projection,
                dtype=dtype,
            )
            up_blocks.append(up_block)
            prev_output_channel = output_channel
        self.up_blocks = ModuleList(up_blocks)
        # out
        self.conv_norm_out = GroupNorm(num_channels=block_out_channels[0],
                                       num_groups=norm_num_groups,
                                       eps=norm_eps,
                                       dtype=dtype)
        self.conv_act = silu
        self.conv_out = Conv2d(block_out_channels[0],
                               out_channels, (3, 3),
                               padding=(1, 1),
                               dtype=dtype)

    def forward(self,
                sample,
                timesteps,
                encoder_hidden_states,
                text_embeds=None,
                time_ids=None):
        # time
        t_emb = self.time_proj(timesteps)
        emb = self.time_embedding(t_emb)

        aug_emb = None
        if self.addition_embed_type == "text_time":
            assert text_embeds is not None and time_ids is not None
            time_embeds = self.add_time_proj(time_ids.view([-1]))
            time_embeds = time_embeds.view([text_embeds.shape[0], -1])
            add_embeds = concat([text_embeds, time_embeds], dim=1)
            add_embeds = cast(add_embeds, emb.dtype)
            aug_emb = self.add_embedding(add_embeds)

        emb = emb + aug_emb if aug_emb is not None else emb

        sample = self.conv_in(sample)

        down_block_res_samples = (sample, )
        for downsample_block in self.down_blocks:

            if hasattr(
                    downsample_block,
                    "attentions") and downsample_block.attentions is not None:

                sample, res_samples = downsample_block(
                    hidden_states=sample,
                    temb=emb,
                    encoder_hidden_states=encoder_hidden_states)
            else:
                sample, res_samples = downsample_block(hidden_states=sample,
                                                       temb=emb)
            down_block_res_samples += res_samples

        sample = self.mid_block(sample,
                                emb,
                                encoder_hidden_states=encoder_hidden_states)

        for upsample_block in self.up_blocks:

            res_samples = down_block_res_samples[-len(upsample_block.resnets):]
            down_block_res_samples = down_block_res_samples[:-len(upsample_block
                                                                  .resnets)]

            if hasattr(upsample_block,
                       "attentions") and upsample_block.attentions is not None:
                sample = upsample_block(
                    hidden_states=sample,
                    temb=emb,
                    res_hidden_states_tuple=res_samples,
                    encoder_hidden_states=encoder_hidden_states,
                )
            else:
                sample = upsample_block(hidden_states=sample,
                                        temb=emb,
                                        res_hidden_states_tuple=res_samples)

        sample = self.conv_norm_out(sample)
        sample = self.conv_act(sample)
        sample = self.conv_out(sample)

        return sample
