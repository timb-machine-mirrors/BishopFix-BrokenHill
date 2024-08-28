import gc

import numpy as np
import psutil
import re
import torch
import torch.nn as nn
from transformers import AutoModelForCausalLM
from transformers import AutoTokenizer

from llm_attacks_bishopfox import get_decoded_token
from llm_attacks_bishopfox import get_decoded_tokens
from llm_attacks_bishopfox import get_embedding_matrix
from llm_attacks_bishopfox import get_embeddings 
from llm_attacks_bishopfox import get_encoded_token 
from llm_attacks_bishopfox import get_encoded_tokens 

from llm_attacks_bishopfox.attack.attack_classes import AdversarialContent
from llm_attacks_bishopfox.attack.attack_classes import AdversarialContentList


def print_stats(function_name):
    print(f"---")
    print(f"[{function_name}] Resource statistics")
    mem_info = psutil.Process().memory_info()
    print(f"System: {mem_info}")
    for i in range(torch.cuda.device_count()):
        cuda_device_name = torch.cuda.get_device_properties(i).name
        cuda_device_total_memory = torch.cuda.get_device_properties(i).total_memory
        cuda_device_reserved_memory = torch.cuda.memory_reserved(i)
        cuda_device_reserved_allocated_memory = torch.cuda.memory_allocated(i)
        cuda_device_reserved_unallocated_memory = cuda_device_reserved_memory - cuda_device_reserved_allocated_memory
        print(f"CUDA device {i} ({cuda_device_name}) - Total memory: {cuda_device_total_memory}, reserved memory: {cuda_device_reserved_memory}, reserved allocated memory: {cuda_device_reserved_allocated_memory}, reserved unallocated memory: {cuda_device_reserved_unallocated_memory}")
    print(f"---")

def create_new_quantized_tensor(default_value, size, device, quantized_dtype, scale, zero_point):
    temp_dtype = torch.uint8
    if quantized_dtype == torch.qint8:
        temp_dtype = torch.int8
    if quantized_dtype == torch.qint32:
        temp_dtype = torch.int32
    result_int = torch.randint(default_value, (default_value + 1), size=size, device=device, dtype=temp_dtype)

    result = torch._make_per_tensor_quantized_tensor(result_int, scale, zero_point) 
    return result

def get_first_value_from_tensor(t):
    if isinstance(t, torch.Tensor):
        tensor_item = t.tolist()
        if len(tensor_item) > 1:
            tensor_item = tensor_item[0]
        return get_first_value_from_tensor(tensor_item)
    return t    

def token_gradients(model, input_ids, input_slice, target_slice, loss_slice):

    """
    Computes gradients of the loss with respect to the coordinates.
    
    Parameters
    ----------
    model : Transformer Model
        The transformer model to be used.
    input_ids : torch.Tensor
        The input sequence in the form of token ids.
    input_slice : slice
        The slice of the input sequence for which gradients need to be computed.
    target_slice : slice
        The slice of the input sequence to be used as targets.
    loss_slice : slice
        The slice of the logits to be used for computing the loss.

    Returns
    -------
    torch.Tensor
        The gradients of each token in the input_slice with respect to the loss.
    """

    #print_stats("token_gradients")
    #print("[token_gradients] Getting embedding weight matrix")
    #print(f"[token_gradients] Debug: model.model.embed_tokens = {model.model.embed_tokens}")
    embed_weights = get_embedding_matrix(model)
    #print(f"[token_gradients] Debug: embed_weights = {embed_weights}")
    #print_stats("token_gradients")

    #print(f"[token_gradients] Debug: embed_weights.dtype={embed_weights.dtype}")

    quantized_tensors = False
    if embed_weights is not None:
        if hasattr(embed_weights, "quantization_scheme"):
            quantized_tensors = True
        if embed_weights.data is not None and len(embed_weights.data) > 0:
            #print(f"[token_gradients] Debug: type(embed_weights.data) = {type(embed_weights.data)}")
            #print(f"[token_gradients] Debug: embed_weights.data = {embed_weights.data} with attributes: {dir(embed_weights.data)} and variables: {vars(embed_weights.data)}")
            #if hasattr(embed_weights.data, "qscheme"):
            #    print(f"[token_gradients] Debug: embed_weights.data.qscheme = {embed_weights.data.qscheme}")
            if embed_weights.data.is_quantized:
                quantized_tensors = True

    #print("[token_gradients] Getting one_hot")
    one_hot = None
    scales_value = None
    pczp_value = None
    if quantized_tensors:
        scales = embed_weights.data.q_per_channel_scales()
        pczp = embed_weights.data.q_per_channel_zero_points()
        scales_value = get_first_value_from_tensor(scales)
        pczp_value = int(get_first_value_from_tensor(pczp))
        #print(f"[token_gradients] Debug: scales = {scales}, scales_value = {scales_value}, type(scales_value) = {type(scales_value)}, pczp = {pczp}, pczp_value = {pczp_value}, type(pczp_value) = {type(pczp_value)}")
#        one_hot = torch.randint(0, 1, size=(input_ids[input_slice].shape[0],embed_weights.shape[0]), device=model.device, dtype=embed_weights.dtype)
        one_hot = create_new_quantized_tensor(0, (input_ids[input_slice].shape[0],embed_weights.shape[0]), model.device, embed_weights.data.dtype, scales_value, pczp_value)
    else:
        one_hot = torch.zeros(
            input_ids[input_slice].shape[0],
            embed_weights.shape[0],
            device=model.device,
            dtype=embed_weights.dtype
        )
    #print_stats("token_gradients")

    #print("[token_gradients] Getting one_hot scatter")
    one_hot_ones = None
    if quantized_tensors:
#        one_hot_ones = torch.randint(1, 2, size=(one_hot.shape[0],1), device=model.device, dtype=embed_weights.dtype)
        one_hot_ones = create_new_quantized_tensor(1, (one_hot.shape[0],1), model.device, embed_weights.data.dtype, scales_value, pczp_value)

    else:
        one_hot_ones = torch.ones(one_hot.shape[0], 1, device=model.device, dtype=embed_weights.dtype)

    one_hot.scatter_(
        1, 
        input_ids[input_slice].unsqueeze(1),
        one_hot_ones
    )
    #print_stats("token_gradients")

    #print("[token_gradients] one_hot.requires_grad_()")
    one_hot.requires_grad_()
    #print_stats("token_gradients")

    #print("[token_gradients] Getting input_embeds")
    input_embeds = (one_hot @ embed_weights).unsqueeze(0)
    #print_stats("token_gradients")
    
    # now stitch it together with the rest of the embeddings
    #print("[token_gradients] Getting embeddings")
    embeds = get_embeddings(model, input_ids.unsqueeze(0)).detach()
    #print_stats("token_gradients")

    #print("[token_gradients] Getting full_embeds")
    full_embeds = torch.cat(
        [
            embeds[:,:input_slice.start,:], 
            input_embeds, 
            embeds[:,input_slice.stop:,:]
        ], 
        dim=1)
    #print_stats("token_gradients")

    #print("[token_gradients] converting full_embeds to float32 because that's what logits() expects")
    #full_embeds = full_embeds.to(torch.float32)
    #print_stats("token_gradients")

    #print(f"[token_gradients] Debug: full_embeds.dtype: {full_embeds.dtype}")
    
    #print("[token_gradients] Getting logits")
    logits = model(inputs_embeds=full_embeds).logits
    #print_stats("token_gradients")

    #print("[token_gradients] Getting targets")
    targets = input_ids[target_slice]
    #print_stats("token_gradients")

    #print("[token_gradients] Getting loss")
    loss = nn.CrossEntropyLoss()(logits[0,loss_slice,:], targets)
    #print_stats("token_gradients")

    #print("[token_gradients] loss.backward()")
    loss.backward()
    #print_stats("token_gradients")
    
    if one_hot.grad is not None:
        #print("[token_gradients] Cloning one_hot.grad")
        grad = one_hot.grad.clone()
        #print_stats("token_gradients")

        #print("[token_gradients] Getting gradients")
        grad = grad / grad.norm(dim=-1, keepdim=True)
        #print_stats("token_gradients")
        return grad

    print("Error: one_hot.grad is None")
    return None

def sample_control(attack_params, adversarial_content_manager, current_adversarial_content, grad, batch_size, topk=256, not_allowed_tokens=None, random_seed = None):

    new_adversarial_token_ids = None

    if random_seed is not None:
        torch.manual_seed(random_seed)
        torch.cuda.manual_seed_all(random_seed)

    if grad is not None:
        if not_allowed_tokens is not None:
            grad[:, not_allowed_tokens.to(grad.device)] = np.infty

        top_indices = (-grad).topk(topk, dim=1).indices
        current_adversarial_content_token_ids_device = torch.tensor(current_adversarial_content.token_ids, device = attack_params.device).to(grad.device)

        original_adversarial_content_token_ids_device = current_adversarial_content_token_ids_device.repeat(batch_size, 1)

        #print(f"[sample_control] Debug: current_adversarial_content_token_ids_device = {current_adversarial_content_token_ids_device}, original_adversarial_content_token_ids_device = {original_adversarial_content_token_ids_device}, top_indices = {top_indices}")

        new_token_pos = torch.arange(
            0, 
            len(current_adversarial_content_token_ids_device), 
            len(current_adversarial_content_token_ids_device) / batch_size,
            device=grad.device
        ).type(torch.int64)
        rand_ints = torch.randint(0, topk, (batch_size, 1), device=grad.device)
        #print(f"[sample_control] Debug: new_token_pos = {new_token_pos}, rand_ints = {rand_ints}")
        new_token_val = torch.gather(
            top_indices[new_token_pos], 1, 
            rand_ints
        )
        #print(f"[sample_control] Debug: new_token_val = {new_token_val}")
        new_adversarial_token_ids = original_adversarial_content_token_ids_device.scatter_(1, new_token_pos.unsqueeze(-1), new_token_val)
        #print(f"[sample_control] Debug: new_adversarial_token_ids = {new_adversarial_token_ids}")

    if random_seed is not None:
        torch.manual_seed(attack_params.torch_manual_seed)
        torch.cuda.manual_seed_all(attack_params.torch_cuda_manual_seed_all)
    
    result = AdversarialContentList()
    
    for i in range(new_adversarial_token_ids.shape[0]):
        new_candidate = AdversarialContent.from_token_ids(adversarial_content_manager, new_adversarial_token_ids[i].tolist())
        result.append_if_new(new_candidate)

    return result

def get_filtered_cands(adversarial_content_manager, new_adversarial_content_list, previous_adversarial_values, filter_cand=True, current_adversarial_content = None, filter_regex = None, filter_repetitive_tokens = None, filter_repetitive_lines = None, filter_newline_limit = None, replace_newline_characters = None, attempt_to_keep_token_count_consistent = False, candidate_filter_tokens_min = None, candidate_filter_tokens_max = None):
    result = AdversarialContentList()
    filtered_count = 0
    if new_adversarial_content_list is None:
        return result
    for i in range(len(new_adversarial_content_list.adversarial_content)):
        #print(f"[get_filtered_cands] Debug: i = {i}")
        #print(f"[get_filtered_cands] Debug: new_adversarial_content_list.adversarial_content[i] = {new_adversarial_content_list.adversarial_content[i].get_short_description()}")
        adversarial_candidate = new_adversarial_content_list.adversarial_content[i].copy()
        if adversarial_candidate is not None and adversarial_candidate.as_string is not None:
            #print(f"[get_filtered_cands] Debug: adversarial_candidate = '{adversarial_candidate.get_short_description()}', current_adversarial_content = '{current_adversarial_content.get_short_description()}', control_cand[i] = '{control_cand[i]}'")
            include_candidate = True
            if filter_cand:
                include_candidate = False
                
                if not adversarial_candidate.is_match(current_adversarial_content):
                    include_candidate = True
                #else:
                    #print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate.get_short_description()}' because it was equivalent to the current control value '{current_adversarial_content.get_short_description()}'.")
                if include_candidate:
                    if previous_adversarial_values.contains_adversarial_content(adversarial_candidate):
                        include_candidate = False
                    #else:
                    #    print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate.get_short_description()}' because it was equivalent to a previous adversarial value.")
                if include_candidate:
                    #token_input_ids = adversarial_content_manager.tokenizer(decoded_str, add_special_tokens=False).input_ids
                    if include_candidate:
                        
                        candidate_token_count = len(adversarial_candidate.token_ids)
                        current_adversarial_content_token_count = len(current_adversarial_content.token_ids)
                        if candidate_filter_tokens_min is not None:
                            if candidate_token_count < candidate_filter_tokens_min:
                                include_candidate = False
                                #print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate.get_short_description()}' because its token count ({candidate_token_count}) was less than the minimum value specified ({candidate_filter_tokens_min}).")
                        if candidate_filter_tokens_max is not None:
                            if candidate_token_count > candidate_filter_tokens_max:
                                include_candidate = False
                                #print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate.get_short_description()}' because its token count ({candidate_token_count}) was greater than the maximum value specified ({candidate_filter_tokens_max}).")
                        if attempt_to_keep_token_count_consistent:
                            if candidate_token_count != current_adversarial_content_token_count:
                                include_candidate = False
                                #print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate.get_short_description()}' because its token count ({candidate_token_count}) was not equal to the length of '{current_adversarial_content.get_short_description()}' ({current_adversarial_content_token_count}).")
                    
                    if include_candidate:
                    
                        #print(f"[get_filtered_cands] Debug: appending '{adversarial_candidate.get_short_description()}' to candidate list because it passsed the filter")
                        
                        if filter_newline_limit is not None:
                            newline_character_count = 0
                            for newline_character in ["\x0a", "\x0d"]:
                                if newline_character in current_adversarial_content.as_string:
                                    for current_char in current_adversarial_content.as_string:
                                        if current_char == newline_character:
                                            newline_character_count += 1
                            if newline_character_count > filter_newline_limit:
                                include_candidate = False
                                #print(f"[get_filtered_cands] Debug: '{adversarial_candidate.get_short_description()}' rejected due to presence of newline character(s)")
                        if include_candidate and filter_regex is not None:
                            if filter_regex.search(current_adversarial_content.as_string):
                                dummy = 1
                                #print(f"[get_filtered_cands] Debug: '{adversarial_candidate.get_short_description()}' passsed the regular expression filter")
                            else:
                                include_candidate = False
                                #print(f"[get_filtered_cands] Debug: '{adversarial_candidate.get_short_description()}' failed the regular expression filter")
                        if include_candidate and filter_repetitive_tokens is not None and filter_repetitive_tokens > 0:
                            token_counts = {}
                            already_notified_tokens = []
                            for c_token in token_input_ids:
                                t_count = 1
                                if c_token in token_counts:
                                    t_count = token_counts[c_token] + 1
                                    if t_count >= filter_repetitive_tokens:
                                        include_candidate = False
                                        if c_token not in already_notified_tokens:
                                            already_notified_tokens.append(c_token)
                                            #print(f"[get_filtered_cands] Debug: '{adversarial_candidate.get_short_description()}' rejected because it had more than {filter_repetitive_tokens} occurrences of the line '{c_token}'")
                                token_counts[c_token] = t_count
                            #if include_candidate:
                            #    print(f"[get_filtered_cands] Debug: '{adversarial_candidate.get_short_description()}' passed the repetitive line filter.")
                        if include_candidate and filter_repetitive_lines is not None and filter_repetitive_lines > 0:
                            candidate_lines = current_adversarial_content.as_string.splitlines()
                            token_counts = {}
                            already_notified_tokens = []
                            for c_line in candidate_lines:
                                t_count = 1
                                if c_line in token_counts:
                                    t_count = token_counts[c_line] + 1
                                    if t_count >= filter_repetitive_lines:
                                        include_candidate = False
                                        if c_line not in already_notified_tokens:
                                            already_notified_tokens.append(c_line)
                                            #print(f"[get_filtered_cands] Debug: '{adversarial_candidate.get_short_description()}' rejected because it had more than {filter_repetitive_lines} occurrences of the line '{c_line}'")
                                token_counts[c_line] = t_count
                            #if include_candidate:
                            #    print(f"[get_filtered_cands] Debug: '{adversarial_candidate.get_short_description()}' passed the repetitive line filter.")
                            
                
            if include_candidate:
                if replace_newline_characters is not None:
                    decoded_str = adversarial_candidate.as_string
                    decoded_str = decoded_str.replace("\n", replace_newline_characters)
                    decoded_str = decoded_str.replace("\r", replace_newline_characters)
                    if decoded_str != adversarial_candidate.as_string:
                        adversarial_candidate = AdversarialContent.from_string(adversarial_content_manager, decoded_str)
                #print(f"[get_filtered_cands] Debug: appending '{adversarial_candidate.get_short_description()}' to candidate list.")
                result.append_if_new(adversarial_candidate)
            else:
                #print(f"[get_filtered_cands] Debug: not appending '{adversarial_candidate.get_short_description()}' to candidate list because it was filtered out.")
                filtered_count += 1

    #print(f"[get_filtered_cands] Debug: control_cand = {control_cand}, cands = {cands}")

    if filter_cand:
        len_result_adversarial_content = len(result.adversarial_content)
        if len_result_adversarial_content == 0:
            dummy = 1
            #print(f"[get_filtered_cands] Warning: no candidates found")
        else:
            # I *think* this step is supposed to append copies of the last entry in the list enough times to make the new list as long as the original list
            #cands = cands + [cands[-1]] * (len(control_cand) - len(cands))
            # TKTK: try taking this out, because it seems weird to have to do this
            if len_result_adversarial_content < len(new_adversarial_content_list.adversarial_content):
                while len(result.adversarial_content) < len(new_adversarial_content_list.adversarial_content):
                    result.adversarial_content.append(result.adversarial_content[-1].copy())
                    
            #print(f"[get_filtered_cands] Warning: {round(filtered_count / len(control_cand), 2)} control candidates were not valid")
    return result


def get_logits(*, model, tokenizer, input_ids, adversarial_content, adversarial_candidate_list = None, return_ids = False, batch_size=512):
    
    if adversarial_candidate_list is None or len(adversarial_candidate_list.adversarial_content) < 1:
        raise ValueError(f"adversarial_candidate_list must be an AdversarialContentList with at least 1 entry. Got empty array or null.")

    test_ids = None
    nested_ids = None

    number_of_adversarial_token_ids = len(adversarial_content.token_ids)

    max_len = number_of_adversarial_token_ids
    test_ids = []
    for i in range(0, len(adversarial_candidate_list.adversarial_content)):
        #tid = torch.tensor(tokenizer(adversarial_candidate_list.adversarial_content[i].token_ids, add_special_tokens=False).input_ids[:max_len], device=model.device)
        tid = torch.tensor(adversarial_candidate_list.adversarial_content[i].token_ids[:max_len], device=model.device)
        #tid = torch.tensor(adversarial_candidate_list.adversarial_content[i].token_ids, device=model.device)
        test_ids.append(tid)

    pad_tok = 0
    while pad_tok in input_ids or any([pad_tok in ids for ids in test_ids]):
        pad_tok += 1
    nested_ids = torch.nested.nested_tensor(test_ids)
    test_ids = torch.nested.to_padded_tensor(nested_ids, pad_tok, (len(test_ids), max_len))

    #decoded_test_ids = get_decoded_tokens(tokenizer, test_ids)
    #print(f"[get_logits] Debug: test_ids = '{test_ids}', decoded_test_ids = '{decoded_test_ids}'")

    if not(test_ids[0].shape[0] == number_of_adversarial_token_ids):
        raise ValueError((
            f"adversarial_candidate_list must have shape "
            f"(n, {number_of_adversarial_token_ids}), " 
            f"got {test_ids.shape}"
        ))

    locs = torch.arange(0, number_of_adversarial_token_ids).repeat(test_ids.shape[0], 1).to(model.device)
    ids = torch.scatter(
        input_ids.unsqueeze(0).repeat(test_ids.shape[0], 1).to(model.device),
        1,
        locs,
        test_ids
    )
    if pad_tok >= 0:
        attn_mask = (ids != pad_tok).type(ids.dtype)
    else:
        attn_mask = None

    if return_ids:
        del locs, test_ids ; gc.collect()
        result1 = forward(model=model, tokenizer = tokenizer, input_ids=ids, attention_mask=attn_mask, batch_size=batch_size)
        
        #print(f"[get_logits] Debug: returning result1 = '{result1}', ids = '{ids}', attn_mask = '{attn_mask}'")

        return result1, ids
    else:
        del locs, test_ids
        logits = forward(model=model, tokenizer = tokenizer, input_ids=ids, attention_mask=attn_mask, batch_size=batch_size)
        del ids ; gc.collect()
        
        #print(f"[get_logits] Debug: returning logits = '{logits}', attn_mask = '{attn_mask}'")
        
        return logits
    

def forward(*, model, tokenizer, input_ids, attention_mask, batch_size=512):

    logits = []
    for i in range(0, input_ids.shape[0], batch_size):
        
        batch_input_ids = input_ids[i:i+batch_size]
        if attention_mask is not None:
            batch_attention_mask = attention_mask[i:i+batch_size]
        else:
            batch_attention_mask = None

        model_result = model(input_ids=batch_input_ids, attention_mask=batch_attention_mask)
        #model_result_decoded = get_decoded_tokens(tokenizer, model_result)
        #print(f"[forward] Debug: getting logits for model_result = '{model_result}', model_result_decoded = '{model_result_decoded}'")
        #print(f"[forward] Debug: getting logits for model_result = '{model_result}'")

        model_result_logits = model_result.logits

        logits.append(model_result_logits)

        gc.collect()

    del batch_input_ids, batch_attention_mask
    
    return torch.cat(logits, dim=0)

# In this function, the logits returned by get_logits and forward are compared against the token IDs returned by get_logits
# ...which seems to correspond to the token IDs that represent the target output, repeated enough times to equal the length of the first entry in the list of candidate values? I think?
# If I understand the goal here, it's to treat the target tokens as coordinates and figure out how far away the candidate tokens are from them?
# ...because I don't see anywhere that the generated output tokens are compared to the target tokens, which is what I originally assumed was the goal.
def target_loss(logits, ids, input_id_data, tokenizer):
    crit = nn.CrossEntropyLoss(reduction='none')
    # [blincoln] Testing out my theory that the -1 offset is incorrect
    # [blincoln] also corrected this to use the loss slice returned by get_prompt for consistency instead of redefining it here using the same logic as get_prompt
    #loss_slice = slice(target_slice.start-1, target_slice.stop-1)
    # input_id_data.slice_data.target
    #loss_slice = slice(target_slice.start, target_slice.stop)
    #logits_sliced = logits[:,loss_slice,:]
    logits_sliced = logits[:,input_id_data.slice_data.loss,:]
    logits_sliced_transposed = logits_sliced.transpose(1,2)
    ids_sliced = ids[:,input_id_data.slice_data.target]
    
    #ids_sliced_decoded = get_decoded_tokens(tokenizer, ids_sliced)
    #print(f"[target_loss] Debug: calculating cross-entropy loss. logits_sliced = '{logits_sliced}', logits_sliced_transposed = '{logits_sliced_transposed}', ids_sliced = '{ids_sliced}', ids_sliced_decoded = '{ids_sliced_decoded}'")

    loss = crit(logits_sliced_transposed, ids_sliced)
    return loss.mean(dim=-1)

def get_missing_pad_token_names():
    result = [  "unk", 
                "bos",
                "eos" ]
    return result

def get_missing_pad_token_replacement(tokenizer, replacement_name):
    allowed_names = get_missing_pad_token_names()
    if replacement_name not in get_missing_pad_token_names(allowed_names):
        raise Exception(f"Unrecognized padding token replacement name: '{replacement_name}' - must be one of '{allowed_names}'")
    result = None
    if replacement_name == "bos":
        result = tokenizer.bos_token
    if replacement_name == "eos":
        result = tokenizer.eos_token
    if replacement_name == "unk":
        result = tokenizer.unk_token

def load_model_and_tokenizer(model_path, tokenizer_path=None, device='cuda:0', dtype=torch.float16, trust_remote_code=True, ignore_mismatched_sizes=False, enable_hardcoded_tokenizer_workarounds = False, missing_pad_token_replacement = None, **kwargs):
    #print(f"[load_model_and_tokenizer] Debug: model_path = '{model_path}', tokenizer_path = '{tokenizer_path}', device = '{device}', dtype = {dtype}, trust_remote_code = {trust_remote_code}, ignore_mismatched_sizes = {ignore_mismatched_sizes}")

    #if ignore_mismatched_sizes:
    #    kwargs["ignore_mismatched_sizes"] = True

    model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=dtype,
            trust_remote_code=trust_remote_code,
            ignore_mismatched_sizes=ignore_mismatched_sizes,
            **kwargs
        ).to(device).eval()
    
    
    tokenizer_path_to_load = model_path
    if tokenizer_path is not None:
        tokenizer_path_to_load = tokenizer_path
    
    #print(f"[load_model_and_tokenizer] Debug: tokenizer_path = '{tokenizer_path}', model_path = '{model_path}'")

    tokenizer = None
    
    #is_mamba = args.model_name.startswith("state-spaces/mamba-")
    #    if is_mamba:
    #tokenizer = AutoTokenizer.from_pretrained("EleutherAI/gpt-neox-20b")
    #model = MambaLMHeadModel.from_pretrained(args.model_name, device=device, dtype=dtype)
    
    try:
        tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_path_to_load,
            trust_remote_code=trust_remote_code,
            use_fast=False
        )
    except Exception as e:
        handled = False
        #if isinstance(e, ValueError):
        if 2 > 1:
            print(f"[load_model_and_tokenizer] Warning: unable to load standard tokenizer from '{tokenizer_path_to_load}', attempting to fall back to fast tokenizer. The exception thrown when loading the standard tokenizer was: {e}")
            try:
                tokenizer = AutoTokenizer.from_pretrained(
                    tokenizer_path_to_load,
                    trust_remote_code=trust_remote_code,
                    use_fast=True
                )
                handled = True
            except Exception as e2:
                print(f"[load_model_and_tokenizer] Error loading both standard and fast tokenizers from '{tokenizer_path_to_load}': '{e}', '{e2}'")
                raise e        
        if not handled:
            print(f"[load_model_and_tokenizer] Error loading tokenizer from '{tokenizer_path_to_load}': '{e}'")
            raise e
    
    if enable_hardcoded_tokenizer_workarounds:
        if 'oasst-sft-6-llama-30b' in tokenizer_path_to_load:
            tokenizer.bos_token_id = 1
            tokenizer.unk_token_id = 0
        if 'guanaco' in tokenizer_path_to_load:
            tokenizer.eos_token_id = 2
            tokenizer.unk_token_id = 0
        if 'llama-2' in tokenizer_path_to_load:
            tokenizer.pad_token = tokenizer.unk_token
            tokenizer.padding_side = 'left'
        if 'falcon' in tokenizer_path_to_load:
            tokenizer.padding_side = 'left'
            
    if not tokenizer.pad_token:
        if missing_pad_token_replacement is not None:
            tokenizer.pad_token = get_missing_pad_token_replacement(tokenizer, missing_pad_token_replacement)
            print(f"[load_model_and_tokenizer] Warning: the tokenizer in '{tokenizer_path_to_load}' does not have a pad_token value defined. Using the alternative value '{missing_pad_token_replacement}' specified by the operator. If you encounter errors or unexpected results, consider specifying a different --missing-pad-token-replacement value on the command line.")
        else:
            print(f"[load_model_and_tokenizer] Warning: the tokenizer in '{tokenizer_path_to_load}' does not have a pad_token value defined. If you encounter errors or unexpected results, consider specifying a --missing-pad-token-replacement value on the command line.")
    
    return model, tokenizer
