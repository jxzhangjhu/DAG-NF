#!/bin/bash
#SBATCH --gres=gpu:2
#SBATCH --cpus-per-gpu=2
#SBATCH --job-name "DAG-NF-Image"
#SBATCH --mem=12G
#SBATCH --time=144:00:00
#SBATCH --output="DAG-NF-$2-%j.out"

args=("$@")
supp_args=$(printf "%s "  "${args[@]:1}")

source activate UMNN
python ImageExperiments.py -dataset $1 -nb_gpus 2 $supp_args -dataset_root /scratch/users/$USER