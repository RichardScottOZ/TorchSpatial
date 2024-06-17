#!/bin/bash

DIR=../models/xyz/

ENC=xyz

DATA=yfcc
META=ebird_meta
EVALDATA=test

DEVICE=cuda:3

LR=0.002
LAYER=1
HIDDIM=512
FREQ=64
MINR=0.0005
MAXR=1
EPOCH=100

ACT=relu
RATIO=1.0


python3 train_unsuper.py \
    --spa_enc_type $ENC \
    --meta_type $META\
    --dataset $DATA \
    --eval_split $EVALDATA \
    --frequency_num $FREQ \
    --max_radius $MAXR \
    --min_radius $MINR \
    --num_hidden_layer $LAYER \
    --hidden_dim $HIDDIM \
    --spa_f_act $ACT \
    --unsuper_lr 0.1 \
    --lr $LR \
    --model_dir $DIR \
    --num_epochs $EPOCH \
    --train_sample_ratio $RATIO \
    --device $DEVICE
