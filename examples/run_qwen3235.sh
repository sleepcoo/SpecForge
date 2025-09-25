

export MODEL_PATH=Qwen/Qwen3-235B-A22B-Thinking-2507-FP8
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
ROOT_DIR=$(dirname $SCRIPT_DIR)
export NUM_GPUS=8
export CHAT_TEMPLATE=qwen
torchrun \
    --standalone \
    --nproc_per_node $NUM_GPUS \
    scripts/train_eagle3_online.py \
    --target-model-path $MODEL_PATH \
    --model-path $MODEL_PATH \
    --draft-model-config /sgl-workspace/SpecForge/configs/qwen3-235B-A22B-eagle3.json \
    --train-data-path /sgl-workspace/SpecForge/cache/dataset/combined_train.jsonl \
    --tp-size $NUM_GPUS \
    --output-dir  $ROOT_DIR/outputs/wen3-235B-A22B-eagle3 \
    --num-epochs 1 \
    --batch-size 1 \
    --learning-rate 5e-5 \
    --draft-attention-backend flex_attention \
    --draft-global-batch-size 8 \
    --max-length 2048 \
    --chat-template $CHAT_TEMPLATE \
    --cache-dir $ROOT_DIR/cache \
    --mem-frac=0.6 \
    --total-steps=800000 \
    --warmup-ratio=0.015 \
    --dist-timeout=40 \
    --resume \
    --build-dataset-num-proc 32 