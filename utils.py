from src.llmtuner import run_exp
import argparse
import json
import random
import os
import subprocess
import time
import copy
import json
import math
import numpy as np
import pandas as pd
from tqdm import tqdm
from openai import OpenAI
from huggingface_hub import login

def jsonl_to_json(jsonl_file_path, output_json_file_path):
    with open(jsonl_file_path, 'r') as jsonl_file:
        lines = jsonl_file.readlines()

    json_data = [json.loads(line.strip()) for line in lines]
    with open(output_json_file_path, 'w') as json_file:
        json.dump(json_data, json_file, indent=4)

def run_cli_command(command):
    os.system(command)

def run_server(cmd_string):
    try:
        server_process = subprocess.Popen(cmd_string, shell=True)
        return server_process
    except Exception as e:
        print(f"Error starting server: {e}")
        return None

def shutdown_server(process):
    try:
        process.terminate()
        print("Server shutdown successfully.")
    except Exception as e:
        print(f"Error shutting down server: {e}")

def find_and_kill_process(command):
    find_pid_command = f"""pgrep -af "{command}" """
    pid_output = subprocess.check_output(find_pid_command, shell=True)
    pid_lines = pid_output.decode().splitlines()
    pids = [line.split()[0] for line in pid_lines]

    print("PID(s) of the process:")
    print(pids)

    if pids:
        kill_pid_command = f"kill -9 {' '.join(pids)}"
        subprocess.run(kill_pid_command, shell=True)
        print("Process(es) killed.")
    else:
        print("No matching process found.")

def eliminate_outcome(old_file, new_file):
    with open(old_file, 'r') as f:
        data = json.load(f)

    filtered_data = [entry for entry in data if entry['instruction'] not in ["I will adopt the animal.", "I will not adopt the animal."]]
    with open(new_file, 'w') as file:
        json.dump(filtered_data, file, indent=4)
    print("Data has been updated and saved.")
            
def load_json(file_path):
    with open(file_path, 'r') as file:
        return json.load(file)

def save_json(data, file_path):
    with open(file_path, 'w') as file:
        json.dump(data, file, indent=4)

def add_new_dataset_info(dataset_info_path, name, path):
    # Read data from dataset_info.json
    with open(dataset_info_path, 'r') as file:
        data = json.load(file)

    if name in data:
        del data[name]  # Remove the existing entry if it exists

    data[name] = {
        "file_name": path,
        "columns": {
            "prompt": "instruction",
            "query": "input",
            "response": "output",
            "history": "history"
        }
    }

    # Save new data info
    with open(dataset_info_path, 'w') as outfile:
        json.dump(data, outfile, indent=4)

def expand_dataset(args, training_data_path, n, k, num_repeat, output_file_path, adapter_path):
    
    training_data = load_json(training_data_path)
    all_data = []

    histories = [[] for _ in range(n * num_repeat)]
    for iteration in range(1, k + 1):
        batch_samples = []
        for p in range(n):
            selected_entry = random.sample(training_data, num_repeat)
            
            for i, entry in enumerate(selected_entry):
                new_entry = {
                    "instruction": entry['instruction'],
                    "input": "",
                    "output": "",
                    "history": histories[p * num_repeat + i].copy()
                }
                batch_samples.append(new_entry)
        
        inference_path = 'data/animal/inference'
        if os.path.exists(inference_path) is False:
            os.mkdir(inference_path)
        
        batch_file_path = f"{inference_path}/batch_data_iteration_{iteration}.json"
        save_json(batch_samples, batch_file_path)
        add_new_dataset_info(args.data_info_path, f'batch_data_iteration_{iteration}', os.path.relpath(batch_file_path, 'data'))
        print(f"Batch data for iteration {iteration} saved to {batch_file_path}")

        perform_inference(args, adapter_path, f'batch_data_iteration_{iteration}')
        predictions_data_path = f"{args.output_dir}/predict/generated_predictions_batch_data_iteration_{iteration}.json"
        predictions_data = load_json(predictions_data_path)
        
        for i in range(0, len(predictions_data)):
            batch_samples[i]['output'] = predictions_data[i]['predict']
            histories[i].append([batch_samples[i]['instruction'], batch_samples[i]['output']])

        all_data.extend(batch_samples)
        save_json(all_data, output_file_path)
        print(f"Iteration {iteration} completed and saved to {output_file_path}")

    return all_data
    
def perform_inference(args, sft_full_path, testset):
    if args.is_using_vllm:
        template = "default" if "default" in args.template.lower() else "llama2"

        deploy_command = f"""CUDA_VISIBLE_DEVICES={args.gpu_ids} API_PORT={args.api_port} python src/api_demo.py \
            --model_name_or_path {sft_full_path} \
            --template {template} \
            --infer_backend vllm \
            --vllm_enforce_eager"""
        
        print("Deploying LLM...")
        server_process = run_server(deploy_command)
        time.sleep(60)

        # Inference
        client = OpenAI(base_url=f"http://localhost:{args.api_port}/v1", api_key="token-abc123")
        data_info_path = args.data_info_path
        data_info = load_json(data_info_path)
        test_data = load_json(data_info[testset]['file_name'])

        predictions = []
        for sample in tqdm(test_data):
            completion = client.chat.completions.create(
                model=sft_full_path,
                messages=[{"role": "user", "content": sample['instruction']}]
            )
            sample['output'] = completion.choices[0].message.content
            predictions.append(sample)

        # output_file_path = f"{args.dataset_dir}/generated_predictions_{testset}.json"
        # save_json(predictions, output_file_path)
        # print(f"Predictions saved to: {output_file_path}")

        # Shutdown server
        shutdown_server(f"kill {server_process.pid}")
    else:
        predict_output_dir = f"{args.output_dir}/predict"
        generate_text_command = f"""CUDA_VISIBLE_DEVICES={args.gpu_ids} python src/train_bash.py \
            --stage sft \
            --do_predict \
            --model_name_or_path {args.model_name_or_path} \
            --adapter_name_or_path {sft_full_path} \
            --dataset {testset} \
            --dataset_dir {args.dataset_dir} \
            --template {args.template} \
            --finetuning_type {args.finetuning_type} \
            --output_dir {predict_output_dir} \
            --cutoff_len {args.cutoff_len} \
            --overwrite_cache \
            --overwrite_output_dir \
            --preprocessing_num_workers 16 \
            --per_device_eval_batch_size {args.per_device_eval_batch_size} \
            --predict_with_generate
        """
        run_cli_command(generate_text_command)
        jsonl_to_json(f"{predict_output_dir}/generated_predictions.jsonl", f"{predict_output_dir}/generated_predictions.json")
        generated_predictions_data = load_json(f"{predict_output_dir}/generated_predictions.json")
        save_json(generated_predictions_data, f"{predict_output_dir}/generated_predictions_{testset}.json")