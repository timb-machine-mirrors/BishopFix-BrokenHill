#!/bin/env python

import gc

import numpy
import psutil
import re
import torch
import torch.nn as nn
from transformers import AutoModelForCausalLM
from transformers import AutoTokenizer

from llm_attacks_bishopfox.attack.attack_classes import AdversarialContent
from llm_attacks_bishopfox.attack.attack_classes import AdversarialContentList
from llm_attacks_bishopfox.attack.attack_classes import LossAlgorithm
from llm_attacks_bishopfox.attack.attack_classes import ModelDataFormatHandling
from llm_attacks_bishopfox.base.attack_manager import get_embedding_matrix
from llm_attacks_bishopfox.base.attack_manager import get_embeddings 
from llm_attacks_bishopfox.dumpster_fires.trash_fire_tokens import encode_string_for_real_without_any_cowboy_funny_business
from llm_attacks_bishopfox.dumpster_fires.trash_fire_tokens import get_decoded_token
from llm_attacks_bishopfox.dumpster_fires.trash_fire_tokens import get_decoded_tokens
from llm_attacks_bishopfox.dumpster_fires.trash_fire_tokens import get_encoded_token 
from llm_attacks_bishopfox.dumpster_fires.trash_fire_tokens import get_encoded_tokens 

class MellowmaxException(Exception):
    pass

class GradientCreationException(Exception):
    pass
    
class GradientSamplingException(Exception):
    pass
    
class PaddingException(Exception):
    pass

class NullPaddingTokenException(PaddingException):
    pass

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
    
def get_padded_target_token_ids(tokenizer, loss_slice, target_ids):
    #print(f"[get_padded_target_token_ids] Debug: target_ids = {target_ids}, loss_slice = {loss_slice}")
    result = target_ids
    return_tensor = None
    input_is_list_of_lists = False
    
    original_target_ids_length = len(target_ids)
    
    target_ids_as_list = None
    if isinstance(target_ids, list):
        target_ids_as_list = copy.deepcopy(target_ids)
        return_tensor = False
    if isinstance(target_ids, torch.Tensor):
        target_ids_as_list = target_ids.tolist()
        return_tensor = True
    
    if isinstance(return_tensor, type(None)):
        raise PaddingException(f"Couldn't pad the object '{target_ids}' because it was not a list or a tensor.")
    
    if len(target_ids_as_list) == 0:
        return result
    
    len_loss_slice = loss_slice.stop - loss_slice.start
    len_comparison = len(target_ids_as_list)

    # Calls to this function with output from e.g. get_logits are passing a multidimensional array of values that need to be padded
    if isinstance(target_ids_as_list[0], list):
        #print(f"[get_padded_target_token_ids] Debug: target_ids_as_list is a multidimensional array")
        input_is_list_of_lists = True
        len_comparison = len(target_ids_as_list[0])

    #print(f"[get_padded_target_token_ids] Debug: target_ids_as_list = {target_ids_as_list}, len_loss_slice = {len_loss_slice}")
    
    if len_loss_slice > len_comparison:
        if isinstance(tokenizer.pad_token_id, type(None)):
            # This should never occur unless someone is calling this function directly, outside of Broken Hill
            raise NullPaddingTokenException("The current target slice must be padded to match the length of the loss slice, but the tokenizer's padding token ID is None.")
        
        if input_is_list_of_lists:
            for list_entry_num in range(0, len(target_ids_as_list)):
                #print(f"[get_padded_target_token_ids] Debug: target_ids_as_list[list_entry_num] = '{target_ids_as_list[list_entry_num]}' before padding.")
                while len_loss_slice > len(target_ids_as_list[list_entry_num]):
                    try:
                        target_ids_as_list[list_entry_num].append(tokenizer.pad_token_id)
                    except Exception as e:
                        raise PaddingException(f"[get_padded_target_token_ids] exception calling target_ids_as_list[list_entry_num].append(tokenizer.pad_token_id) with target_ids_as_list = '{target_ids_as_list}', list_entry_num = {list_entry_num}, tokenizer.pad_token_id = '{tokenizer.pad_token_id}'.")
                #print(f"[get_padded_target_token_ids] Debug: target_ids_as_list[list_entry_num] = '{target_ids_as_list[list_entry_num]}' after padding.")
        else:
            while len_loss_slice > len(target_ids_as_list):
                target_ids_as_list.append(tokenizer.pad_token_id)
        result = target_ids_as_list
    
    if return_tensor:
        if not isinstance(result, torch.Tensor):
            result = torch.tensor(result, device = target_ids.device)
    
    #print(f"[get_padded_target_token_ids] Debug: original_target_ids_length = {original_target_ids_length}, len(result) = {len(result)}, len(target_ids_as_list) = {len(target_ids_as_list)}, len_loss_slice = {len_loss_slice}, result = '{result}', target_ids_as_list = '{target_ids_as_list}'")
    
    return result

# BEGIN: mellowmax loss function borrowed from nanoGCG
def mellowmax(t: torch.Tensor, alpha = 1.0, dim = -1):
    torch_logsumexp = None
    torch_tensor = None
    torch_log = None
    tensor_data = None
    result = None
    print(f"[mellowmax] Debug: t = '{t}', t.shape = '{t.shape}', t.dtype = {t.dtype}, alpha = {alpha}, dim = {dim}")
    try:
        torch_logsumexp = torch.logsumexp(alpha * t, dim = dim)
        print(f"[mellowmax] Debug: torch_logsumexp = '{torch_logsumexp}'")
    except Exception as e:
        raise MellowmaxException(f"Error calling torch.logsumexp(alpha * t, dim = dim) with alpha = alpha, t = '{t}', dim = '{dim}': {e}")
    try:
        torch_tensor = torch.tensor(t.shape[-1], dtype = t.dtype, device = t.device)
        print(f"[mellowmax] Debug: torch_tensor = '{torch_tensor}'")
    except Exception as e:
        raise MellowmaxException(f"Error calling torch.tensor(t.shape[-1], dtype = t.dtype, device = t.device) with t = '{t}', t.shape = '{t.shape}', dtype = '{dtype}': {e}")
    try:
        torch_log = torch.log(torch_tensor)
        print(f"[mellowmax] Debug: torch_log = '{torch_log}'")
    except Exception as e:
        raise MellowmaxException(f"Error calling torch.log(torch_tensor) with torch_tensor = '{torch_tensor}': {e}")
    try:
        tensor_data = torch_logsumexp - torch_log
        print(f"[mellowmax] Debug: tensor_data = '{tensor_data}'")
    except Exception as e:
        raise MellowmaxException(f"Error calling torch_logsumexp - torch_log with torch_logsumexp = '{torch_logsumexp}', torch_log = '{torch_log}': {e}")
    try:
        result = 1.0 / alpha * (tensor_data)
        print(f"[mellowmax] Debug: result = '{result}'")
    except Exception as e:
        raise MellowmaxException(f"Error calling 1.0 / alpha * (tensor_data) with alpha = alpha, tensor_data = '{tensor_data}': {e}")
    return result
# END: mellowmax loss function borrowed from nanoGCG


def token_gradients(attack_state, input_token_ids_model_device, input_id_data):

    """
    Computes gradients of the loss with respect to the coordinates.
    
    Parameters
    ----------
    input_token_ids_model_device : torch.Tensor
        The input sequence in the form of token ids.
    input_id_data.slice_data.control : slice
        The slice of the input sequence for which gradients need to be computed.
    input_id_data.slice_data.target_output : slice
        The slice of the input sequence to be used as targets.
    input_id_data.slice_data.loss : slice
        The slice of the logits to be used for computing the loss.

    Returns
    -------
    torch.Tensor
        The gradients of each token in input_id_data.slice_data.control with respect to the loss.
    """

    #attack_state.persistable.performance_data.collect_torch_stats("token_gradients")
    #print("[token_gradients] Debug: Getting embedding weight matrix")
    embedding_matrix = get_embedding_matrix(attack_state.model)
    #print(f"[token_gradients] Debug: embedding_matrix = {embedding_matrix}")
    #print(f"[token_gradients] Debug: embedding_matrix.shape = {embedding_matrix.shape}")
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - begin")

    #print(f"[token_gradients] Debug: embedding_matrix.dtype={embedding_matrix.dtype}")

    input_token_ids_gradient_device = input_token_ids_model_device.to(attack_state.gradient_device)

    quantized_tensors = False
    if embedding_matrix is not None:
        if hasattr(embedding_matrix, "quantization_scheme"):
            print(f"[token_gradients] Debug: embedding_matrix has quantization_scheme property, assuming quantized tensors")
            quantized_tensors = True
        if embedding_matrix.data is not None and len(embedding_matrix.data) > 0:
            #print(f"[token_gradients] Debug: type(embedding_matrix.data) = {type(embedding_matrix.data)}")
            #print(f"[token_gradients] Debug: embedding_matrix.data = {embedding_matrix.data} with attributes: {dir(embedding_matrix.data)} and variables: {vars(embedding_matrix.data)}")
            #if hasattr(embedding_matrix.data, "qscheme"):
            #    print(f"[token_gradients] Debug: embedding_matrix.data.qscheme = {embedding_matrix.data.qscheme}")
            if embedding_matrix.data.is_quantized:
                print(f"[token_gradients] Debug: embedding_matrix.data.is_quantized is True, assuming quantized tensors")
                quantized_tensors = True
        else:
            raise GradientCreationException(f"Can't create a gradient when embedding_matrix.data is null or empty.")
    
    # Note: comment/uncomment the next five lines as one block
    #print(f"[token_gradients] Debug: input_id_data.slice_data.control = {input_id_data.slice_data.control}")
    #print(f"[token_gradients] Debug: input_token_ids_gradient_device = {input_token_ids_gradient_device}")
    #input_token_ids_gradient_device_decoded = get_decoded_tokens(attack_state.tokenizer, input_token_ids_gradient_device)
    #print(f"[token_gradients] Debug: input_token_ids_gradient_device_decoded = {input_token_ids_gradient_device_decoded}")
    #print(f"[token_gradients] Debug: input_token_ids_gradient_device[input_id_data.slice_data.control].shape = {input_token_ids_gradient_device[input_id_data.slice_data.control].shape}")
    
    if input_token_ids_gradient_device[input_id_data.slice_data.control].shape[0] < 1:
        raise GradientCreationException(f"Can't create a gradient when the adversarial content ('control') slice of the input ID data has no content.")

    #print("[token_gradients] Debug: Getting one_hot")
    # memory management: one_hot is required until almost the end of this method
    one_hot = None
    scales_value = None
    pczp_value = None
    if quantized_tensors:
        scales = embedding_matrix.data.q_per_channel_scales()
        pczp = embedding_matrix.data.q_per_channel_zero_points()
        scales_value = get_first_value_from_tensor(scales)
        pczp_value = int(get_first_value_from_tensor(pczp))
        #print(f"[token_gradients] Debug: scales = {scales}, scales_value = {scales_value}, type(scales_value) = {type(scales_value)}, pczp = {pczp}, pczp_value = {pczp_value}, type(pczp_value) = {type(pczp_value)}")
        one_hot = create_new_quantized_tensor(0, (input_token_ids_gradient_device[input_id_data.slice_data.control].shape[0],embedding_matrix.shape[0]), attack_state.gradient_device, embedding_matrix.data.dtype, scales_value, pczp_value)
    else:
        try:
            one_hot = torch.zeros(
                input_token_ids_model_device[input_id_data.slice_data.control].shape[0],
                embedding_matrix.shape[0],
                device = attack_state.gradient_device,
                dtype = embedding_matrix.dtype)
        except Exception as e:
            raise GradientCreationException(f"Error calling one_hot = torch.zeros(input_token_ids_gradient_device[input_id_data.slice_data.control].shape[0], embedding_matrix.shape[0], device = attack_state.gradient_device, dtype = embedding_matrix.dtype) with input_token_ids_gradient_device = '{input_token_ids_gradient_device}', input_token_ids_gradient_device[input_id_data.slice_data.control] = '{input_token_ids_gradient_device[input_id_data.slice_data.control]}', input_token_ids_gradient_device[input_id_data.slice_data.control].shape = '{input_token_ids_gradient_device[input_id_data.slice_data.control].shape}', embedding_matrix = '{embedding_matrix}', embedding_matrix.shape = '{embedding_matrix.shape}', dtype = '{dtype}': {e}")        
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - one_hot created")
    #print(f"[token_gradients] Debug: one_hot = {one_hot}")
    #print(f"[token_gradients] Debug: scales_value = {scales_value}")
    #print(f"[token_gradients] Debug: pczp_value = {pczp_value}")
    
    if one_hot.shape[0] < 1:
        raise GradientCreationException(f"Got an empty list when trying to create the one_hot tensor.")

    #print("[token_gradients] Debug: Getting one_hot scatter")
    one_hot_ones = None
    if quantized_tensors:
        one_hot_ones = create_new_quantized_tensor(1, (one_hot.shape[0],1), attack_state.gradient_device, embedding_matrix.data.dtype, scales_value, pczp_value)
    else:
        try:
            one_hot_ones = torch.ones(one_hot.shape[0], 1, device = attack_state.gradient_device, dtype = embedding_matrix.dtype)
        except Exception as e:
            raise GradientCreationException(f"Error calling one_hot_ones = torch.ones(one_hot.shape[0], 1, device = attack_state.gradient_device, dtype = embedding_matrix.dtype) with one_hot = '{one_hot}', one_hot.shape = '{one_hot.shape}', dtype = '{dtype}': {e}")

    #print(f"[token_gradients] Debug: one_hot_ones = {one_hot_ones}")

    one_hot_scatter_input = None
    try:
        one_hot_scatter_input = input_token_ids_gradient_device[input_id_data.slice_data.control].unsqueeze(1)
    except Exception as e:
        raise GradientCreationException(f"Error calling one_hot_scatter_input = input_token_ids_gradient_device[input_id_data.slice_data.control].unsqueeze(1)) with input_token_ids_gradient_device = '{input_token_ids_gradient_device}', input_id_data.slice_data.control = '{input_id_data.slice_data.control}': {e}")

    try:
        one_hot.scatter_(
            1, 
            one_hot_scatter_input,
            one_hot_ones
        )
    except Exception as e:
        raise GradientCreationException(f"Error calling one_hot.scatter_(1, one_hot_scatter_input, one_hot_ones) with one_hot_scatter_input = '{one_hot_scatter_input}', one_hot_ones = '{one_hot_ones}': {e}")
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - one_hot scattered")
    
    #print(f"[token_gradients] Debug: one_hot_ones = {one_hot_ones}")
    
    # one_hot_ones and one_hot_scatter_input are no longer needed
    del one_hot_ones
    del one_hot_scatter_input
    gc.collect()
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - after deleting one_hot_ones")

    #print("[token_gradients] Debug: one_hot.requires_grad_()")
    one_hot.requires_grad_()
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - one_hot.requires_grad_() complete")

    #print("[token_gradients] Debug: Getting input_embeds")
    input_embeds = (one_hot.to(attack_state.model_device) @ embedding_matrix).unsqueeze(0)
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - input_embeds created")

    # embedding_matrix is no longer needed, and occupies about 600 MiB of device memory for a 500M parameter model, so get rid of it now to save memory
    del embedding_matrix
    gc.collect()
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - after deleting embedding_matrix")
    
    #print(f"[token_gradients] Debug: input_embeds = {input_embeds}")
    
    # now stitch it together with the rest of the embeddings
    #print("[token_gradients] Debug: Getting embeddings")
    embeds = None
    try:
        embeds = get_embeddings(attack_state.model, input_token_ids_model_device.unsqueeze(0)).detach()
    except Exception as e:
        raise GradientCreationException(f"Error calling get_embeddings(attack_state.model, input_token_ids_model_device.unsqueeze(0)).detach() with input_token_ids_model_device.unsqueeze(0) = '{input_token_ids_model_device.unsqueeze(0)}': {e}")
    #embeds = embeds.to(attack_state.gradient_device)
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - embeds created")

    #print(f"[token_gradients] Debug: embeds = {embeds}")

    #print("[token_gradients] Debug: Getting full_embeds")
    full_embeds = None
    # try:
        # full_embeds = torch.cat(
            # [
                # embeds[:,:input_id_data.slice_data.control.start,:], 
                # input_embeds, 
                # embeds[:,input_id_data.slice_data.control.stop:,:]
            # ], 
            # dim=1)
    full_embeds = torch.cat(
        [
            embeds[:,:input_id_data.slice_data.control.start,:], 
            input_embeds, 
            embeds[:,input_id_data.slice_data.control.stop:,:]
        ], 
        dim=1)
    # except Exception as e:
        # raise GradientCreationException(f"Error calling torch.cat([embeds[:,:input_id_data.slice_data.control.start,:], input_embeds, embeds[:,input_id_data.slice_data.control.stop:,:]], dim=1) with embeds = '{embeds}', input_embeds = '{input_embeds}': {e}")
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - full_embeds created")

    del embeds
    del input_embeds
    gc.collect()
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - after deleting embeds and input_embeds")

    #print("[token_gradients] converting full_embeds to float32 because that's what logits() expects")
    #full_embeds = full_embeds.to(torch.float32)
    #attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - full_embeds converted to float32")

    #print(f"[token_gradients] Debug: full_embeds = {full_embeds}")
    #print(f"[token_gradients] Debug: full_embeds.dtype: {full_embeds.dtype}")
    
    #print("[token_gradients] Debug: Getting logits")
    logits = None
    try:
        logits = attack_state.model(inputs_embeds = full_embeds).logits.to(attack_state.gradient_device)
    # TKTK: is there a way to limit this up front to just the user input/adversarial content and the messages that follow? That should reduce device memory consumption considerably.
    #logits = attack_state.model(inputs_embeds=full_embeds).logits
    except Exception as e:
        raise GradientCreationException(f"Error calling attack_state.model(inputs_embeds = full_embeds).logits with full_embeds = '{full_embeds}': {e}")
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - logits created")
    #print(f"[token_gradients] Debug: logits = {logits}")
    
    del full_embeds
    gc.collect()
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - after deleting full_embeds")

    #print("[token_gradients] Debug: Getting targets")
    targets = None
    try:
        targets = input_token_ids_gradient_device[input_id_data.slice_data.target_output]
    except Exception as e:
        raise GradientCreationException(f"Error calling input_token_ids_gradient_device[input_id_data.slice_data.target_output] with input_token_ids_gradient_device = '{input_token_ids_gradient_device}', input_id_data.slice_data.target_output = '{input_id_data.slice_data.target_output}': {e}")
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - targets created")
    #print(f"[token_gradients] Debug: targets = {targets}")
    
    del input_token_ids_gradient_device
    gc.collect()
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - after deleting input_token_ids_gradient_device")
    
    # pad the target token IDs, if necessary
    try:
        targets = get_padded_target_token_ids(attack_state.tokenizer, input_id_data.slice_data.loss, targets)
    except Exception as e:
        raise GradientCreationException(f"Error calling get_padded_target_token_ids(attack_state.tokenizer, input_id_data.slice_data.loss, targets) with input_id_data.slice_data.loss = '{input_id_data.slice_data.loss}', targets = '{targets}': {e}")
    # len_loss_slice = input_id_data.slice_data.loss.stop - input_id_data.slice_data.loss.start
    # if len_loss_slice > len(targets):
        # if attack_state.tokenizer.pad_token_id is None:
            # # This should never occur unless someone is calling this function directly, outside of Broken Hill
            # raise NullPaddingTokenException("The current target slice must be padded to match the length of the loss slice, but the tokenizer's padding token ID is None.")
        # targets_as_list = targets.tolist()
        # while len_loss_slice > len(targets_as_list):
            # targets_as_list.append(attack_state.tokenizer.pad_token_id)
        # targets = torch.tensor(targets_as_list, device = targets.device)
    #print(f"[token_gradients] Debug: targets (after padding, if necessary) = {targets}")


    #print("[token_gradients] Debug: Getting loss")
    got_loss = False
    loss = None
    loss_logits = None
    try:
        loss_logits = logits[0,input_id_data.slice_data.loss,:]
    except Exception as e:
        raise GradientCreationException(f"Error calling logits[0,input_id_data.slice_data.loss,:] with input_id_data.slice_data.loss = '{input_id_data.slice_data.loss}': {e}")

    #print(f"[token_gradients] Debug: loss_logits = {loss_logits}")

    del logits
    gc.collect()
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - after deleting logits")

    if attack_state.persistable.attack_params.loss_algorithm == LossAlgorithm.CROSS_ENTROPY:
        try:
            loss = nn.CrossEntropyLoss()(loss_logits, targets)
            got_loss = True
        except Exception as e:
            raise GradientCreationException(f"Error calling nn.CrossEntropyLoss()(loss_logits, targets) with loss_logits = '{loss_logits}', targets = '{targets}': {e}")

    # TKTK: fix this
    # if not got_loss and attack_state.persistable.attack_params.loss_algorithm == LossAlgorithm.MELLOWMAX:
        # label_logits = None
        # try:
            # label_logits = torch.gather(loss_logits, -1, targets.unsqueeze(-1)).squeeze(-1)
        # except Exception as e:
            # raise GradientCreationException(f"Error calling torch.gather(loss_logits, -1, targets.unsqueeze(-1)).squeeze(-1) with loss_logits = '{loss_logits}', targets.unsqueeze(-1) = '{targets.unsqueeze(-1)}': {e}")
        # try:
            # loss = mellowmax(-label_logits, alpha = attack_state.persistable.attack_params.mellowmax_alpha, dim = -1)
            # got_loss = True
        # except Exception as e:
            # raise GradientCreationException(f"Error calling mellowmax(-label_logits, alpha = attack_state.persistable.attack_params.mellowmax_alpha, dim = -1) with label_logits = '{label_logits}', alpha = '{alpha}': {e}")

    if not got_loss:
        print("[token_gradients] Error: unknown loss algorithm '{attack_state.persistable.attack_params.loss_algorithm}'")
        sys.exit(1)
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - loss created")
    
    del targets
    del loss_logits
    gc.collect()
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - after deleting targets and loss_logits")
    
    #print(f"[token_gradients] Debug: loss = {loss}")

    #print("[token_gradients] Debug: loss.backward()")
    #try:
    #    loss.backward()
    # This one operation requires about 1 GiB of PyTorch device memory for a 500M parameter model, regardless of whether it's CPU or GPU
    loss.backward()
    #except Exception as e:
    #    raise GradientCreationException(f"Error calling loss.backward(): {e}")
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - loss.backward() complete")
    
    # del loss
    # gc.collect()
    # attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - after deleting loss")
    
    #print(f"[token_gradients] Debug: loss (after backpropagation) = {loss}")
    
    if one_hot.grad is not None:
        #print("[token_gradients] Debug: Cloning one_hot.grad")
        result_gradient = one_hot.grad.clone()
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - one_hot.grad cloned")
        
        # # TKTK: this is experimental: will it cause issues to delete it now?
        # del one_hot
        # gc.collect()
        # attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - after deleting one_hot")
        
        #print(f"[token_gradients] Debug: result_gradient = {result_gradient}")

        #print("[token_gradients] Debug: Getting gradients")
        try:
            result_gradient = result_gradient / result_gradient.norm(dim=-1, keepdim=True)
        except Exception as e:
            raise GradientCreationException(f"Error calling result_gradient / result_gradient.norm(dim=-1, keepdim=True) with result_gradient = '{result_gradient}', result_gradient.norm(dim=-1, keepdim=True) = '{result_gradient.norm(dim=-1, keepdim=True)}': {e}")
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "token_gradients - result_gradient created")
        #print(f"[token_gradients] Debug: result_gradient (after normalization) = {result_gradient}")
        return result_gradient

    raise GradientCreationException("Error: one_hot.grad is None")

def get_adversarial_content_candidates(attack_state, coordinate_gradient, not_allowed_tokens = None):

    new_adversarial_token_ids = None

    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - begin")

    if coordinate_gradient is not None:
        if not_allowed_tokens is not None:
            coordinate_gradient[:, not_allowed_tokens.to(coordinate_gradient.device)] = numpy.infty

        top_indices = (-coordinate_gradient).topk(attack_state.persistable.attack_params.topk, dim=1).indices
        if top_indices.shape[0] < 1:
            raise GradientSamplingException(f"No top indices were generated from the coordinate gradient. Coordinate gradient was: {coordinate_gradient}.")
        
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - after creating top_indices")
        
        current_adversarial_content_token_ids_gradient_device = torch.tensor(attack_state.persistable.current_adversarial_content.token_ids).to(coordinate_gradient.device)
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - after creating current_adversarial_content_token_ids_gradient_device")

        original_adversarial_content_token_ids_gradient_device = current_adversarial_content_token_ids_gradient_device.repeat(attack_state.persistable.attack_params.new_adversarial_value_candidate_count, 1)
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - after creating original_adversarial_content_token_ids_gradient_device")
        
        new_token_pos = None
        new_token_val = None

        use_nanogcg_sampling_algorithm = False
        if attack_state.persistable.attack_params.number_of_tokens_to_update_every_iteration > 1:
            use_nanogcg_sampling_algorithm = True
        if attack_state.persistable.attack_params.always_use_nanogcg_sampling_algorithm:
            use_nanogcg_sampling_algorithm = True
        
    # TKTK: validate this
        if use_nanogcg_sampling_algorithm:
            #print(f"[get_adversarial_content_candidates] Debug: using nanoGCG sampling algorithm")
            # BEGIN: nanoGCG gradient-sampling algorithm
            random_ids_1 = None
            random_ids_2 = None
            try:
                random_ids_1 = torch.rand((attack_state.persistable.attack_params.new_adversarial_value_candidate_count, len(attack_state.persistable.current_adversarial_content.token_ids)), generator = attack_state.random_number_generators.random_generator_attack_params_gradient_device, device = coordinate_gradient.device)
            except RuntimeError as e:
                raise GradientSamplingException(f"Couldn't generate first set of random IDs: {e}")
            try:
                random_ids_2 = torch.randint(0, attack_state.persistable.attack_params.topk, (attack_state.persistable.attack_params.new_adversarial_value_candidate_count, attack_state.persistable.attack_params.number_of_tokens_to_update_every_iteration, 1), device = coordinate_gradient.device, generator = attack_state.random_number_generators.random_generator_attack_params_gradient_device)
            except RuntimeError as e:
                raise GradientSamplingException(f"Couldn't generate second set of random IDs: {e}")
            try:
                new_token_pos = torch.argsort(random_ids_1)[..., :attack_state.persistable.attack_params.number_of_tokens_to_update_every_iteration]
            except Exception as e:
                raise GradientSamplingException(f"Error calling torch.argsort(random_ids_1)[..., :attack_state.persistable.attack_params.number_of_tokens_to_update_every_iteration] with random_ids_1 = '{random_ids_1}', attack_state.persistable.attack_params.number_of_tokens_to_update_every_iteration = {attack_state.persistable.attack_params.number_of_tokens_to_update_every_iteration}: {e}")
            attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - after creating new_token_pos")
            try:
                new_token_val = torch.gather(
                    top_indices[new_token_pos],
                    2,
                    random_ids_2
                ).squeeze(2)
            except Exception as e:
                raise GradientSamplingException(f"Error calling new_token_val = torch.gather(top_indices[new_token_pos], 2, random_ids_2).squeeze(2) with top_indices = '{top_indices}', new_token_pos = '{new_token_pos}', top_indices[new_token_pos] = '{top_indices[new_token_pos]}', random_ids_2 = '{random_ids_2}': {e}")
            # END: nanoGCG gradient-sampling algorithm
            attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - after creating new_token_val")
        else:
            #print(f"[get_adversarial_content_candidates] Debug: using original sampling algorithm")
            num_adversarial_tokens = len(current_adversarial_content_token_ids_gradient_device)

            try:
                new_token_pos = torch.arange(
                    0, 
                    num_adversarial_tokens, 
                    num_adversarial_tokens / attack_state.persistable.attack_params.new_adversarial_value_candidate_count,
                    device = coordinate_gradient.device
                ).type(torch.int64)
            except Exception as e:
                raise GradientSamplingException(f"Error calling torch.arange(0, num_adversarial_tokens, num_adversarial_tokens / attack_state.persistable.attack_params.new_adversarial_value_candidate_count, device = coordinate_gradient.device) with num_adversarial_tokens = '{num_adversarial_tokens}', attack_state.persistable.attack_params.new_adversarial_value_candidate_count = '{attack_state.persistable.attack_params.new_adversarial_value_candidate_count}', top_indices[new_token_pos] = '{top_indices[new_token_pos]}', random_ids_2 = '{random_ids_2}': {e}")
            attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - after creating new_token_pos")
            num_rand_ints = attack_state.persistable.attack_params.new_adversarial_value_candidate_count
            # There's probably a better way to handle this, but I don't understand the low-level operation here well enough to implement that "better way" yet.
            if top_indices.shape[0] < num_adversarial_tokens:
                print(f"Warning: the number of top token indices ({top_indices.shape[0]}) is less than the current number of adversarial content tokens ({num_adversarial_tokens}). The number of top indices will be looped to create enough values. This usually indicates a problem with the tokens being processed.")
                looped_values = []
                looped_value_number = 0
                while len(looped_values) < num_adversarial_tokens:
                    looped_values.append(top_indices[looped_value_number % top_indices.shape[0]].tolist())
                    looped_value_number += 1
                top_indices = torch.tensor(looped_values, device = coordinate_gradient.device)
                attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - after looping values")
            rand_ints = None
            try:
                rand_ints = torch.randint(0, attack_state.persistable.attack_params.topk, (num_rand_ints, 1), device = coordinate_gradient.device, generator = attack_state.random_number_generators.random_generator_attack_params_gradient_device)
            except Exception as e:
                raise GradientSamplingException(f"Error calling torch.randint(0, attack_state.persistable.attack_params.topk, (num_rand_ints, 1), ...) with attack_state.persistable.attack_params.topk = '{attack_state.persistable.attack_params.topk}', num_rand_ints = '{num_rand_ints}': {e}")
            attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - after creating rand_ints")
            #print(f"[get_adversarial_content_candidates] Debug: new_token_pos = {new_token_pos}, rand_ints = {rand_ints}")
            new_token_val = None
            top_indices_len_1 = top_indices.shape[0] - 1
            new_token_pos_in_bounds_values = []
            new_token_pos_out_of_bounds_values = []
            new_token_pos_values = new_token_pos.tolist()
            for i in range(0, len(new_token_pos_values)):
                if new_token_pos_values[i] > top_indices_len_1:
                    new_token_pos_out_of_bounds_values.append(new_token_pos_values[i])
                else:
                    new_token_pos_in_bounds_values.append(new_token_pos_values[i])
            if len(new_token_pos_out_of_bounds_values) > 0:
                #raise Exception(f"new_token_pos contained the following values, which are less than zero or greater than the upper bound of top_indices ({top_indices_len_1}): {new_token_pos_out_of_bounds_values}.")
                print(f"Warning: new_token_pos contained the following values, which are less than zero or greater than the upper bound of the list of top token indices ({top_indices_len_1}): {new_token_pos_out_of_bounds_values}. This usually indicates a problem with the tokens being processed.")
                #new_token_pos = torch.tensor(new_token_pos_in_bounds_values, device = coordinate_gradient.device)
                looped_values = []
                looped_value_number = 0
                while len(looped_values) < attack_state.persistable.attack_params.new_adversarial_value_candidate_count:
                    looped_values.append(new_token_pos_in_bounds_values[looped_value_number % len(new_token_pos_in_bounds_values)])
                    looped_value_number += 1
                new_token_pos = torch.tensor(looped_values, device = coordinate_gradient.device)
                attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - after creating new_token_pos")
            try:
                new_token_val = torch.gather(
                    top_indices[new_token_pos], 1, 
                    rand_ints
                )
            except Exception as e:
                raise GradientSamplingException(f"Error calling torch.gather(top_indices[new_token_pos], 1, rand_ints) with top_indices = '{top_indices}', new_token_pos = '{new_token_pos}', top_indices[new_token_pos] = '{top_indices[new_token_pos]}', rand_ints = '{rand_ints}': {e}")
            attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - after creating new_token_val")
            new_token_pos = new_token_pos.unsqueeze(-1)

        #print(f"[get_adversarial_content_candidates] Debug: original_adversarial_content_token_ids_gradient_device = {original_adversarial_content_token_ids_gradient_device}")
        #print(f"[get_adversarial_content_candidates] Debug: new_token_pos = {new_token_pos}")
        #print(f"[get_adversarial_content_candidates] Debug: new_token_val = {new_token_val}")
        #new_token_pos_unsqueezed = new_token_pos.unsqueeze(-1)
        #print(f"[get_adversarial_content_candidates] Debug: new_token_pos_unsqueezed = {new_token_pos_unsqueezed}")
        try:
            new_adversarial_token_ids = original_adversarial_content_token_ids_gradient_device.scatter_(1, new_token_pos, new_token_val)
        except Exception as e:
            raise GradientSamplingException(f"Error calling original_adversarial_content_token_ids_gradient_device.scatter_(1, new_token_pos, new_token_val) with original_adversarial_content_token_ids_gradient_device = '{original_adversarial_content_token_ids_gradient_device}', new_token_pos = '{new_token_pos}', new_token_val = '{new_token_val}': {e}")
        #print(f"[get_adversarial_content_candidates] Debug: new_adversarial_token_ids = {new_adversarial_token_ids}")
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_adversarial_content_candidates - after creating new_adversarial_token_ids")
    
    result = AdversarialContentList()
    
    if new_adversarial_token_ids is not None:
        #print(f"[sample_control] Debug: new_adversarial_token_ids = {new_adversarial_token_ids}")
        for i in range(new_adversarial_token_ids.shape[0]):
            #print(f"[sample_control] Debug: new_adversarial_token_ids[{i}] = {new_adversarial_token_ids[i]}")
            new_adversarial_token_ids_as_list = new_adversarial_token_ids[i].tolist()
            if AdversarialContent.token_list_contains_invalid_tokens(attack_state.tokenizer, new_adversarial_token_ids_as_list):
                dummy = 1
                #print(f"[sample_control] Warning: adversarial_candidate '{new_adversarial_token_ids_as_list}' contains a token ID that is outside the valid range for this tokenizer (min = 0, max = {attack_state.tokenizer.vocab_size}). The candidate will be ignored. This may indicate an issue with the attack code, or the tokenizer code.")
            else:
                new_candidate = AdversarialContent.from_token_ids(attack_state.tokenizer, attack_state.adversarial_content_manager.trash_fire_tokens, new_adversarial_token_ids_as_list)
                result.append_if_new(new_candidate)
    return result

def get_filtered_cands(attack_state, new_adversarial_content_list, filter_cand = True):
    result = AdversarialContentList()
    filter_regex = attack_state.persistable.attack_params.get_candidate_filter_regex()
    filtered_count = 0
    filtered_due_to_empty_string = []
    filtered_due_to_already_being_tested = []
    filtered_due_to_insufficient_token_count = []
    filtered_due_to_excessive_token_count = []
    filtered_due_to_nonmatching_token_count = []
    filtered_due_to_containing_newline_characters = []
    filtered_due_to_not_matching_regex = []
    filtered_due_to_repetitive_tokens = []
    filtered_due_to_repetitive_lines = []
    if new_adversarial_content_list is None:
        return result
    len_new_adversarial_content_list = len(new_adversarial_content_list.adversarial_content)
    for i in range(len_new_adversarial_content_list):
        #print(f"[get_filtered_cands] Debug: i = {i}")
        #print(f"[get_filtered_cands] Debug: new_adversarial_content_list.adversarial_content[i] = {new_adversarial_content_list.adversarial_content[i].get_short_description()}")
        adversarial_candidate = new_adversarial_content_list.adversarial_content[i].copy()
        if adversarial_candidate is not None and adversarial_candidate.as_string is not None:
            #adversarial_candidate_message_represenation = adversarial_candidate.adversarial_candidate.get_short_description()
            adversarial_candidate_message_represenation = adversarial_candidate.as_string
            #print(f"[get_filtered_cands] Debug: adversarial_candidate = '{adversarial_candidate.get_short_description()}', attack_state.persistable.current_adversarial_content = '{attack_state.persistable.current_adversarial_content.get_short_description()}', control_cand[i] = '{control_cand[i]}'")
            include_candidate = True
            # make sure the LLM sorcery hasn't accidentally introduced a token ID that's outside of the valid range
            if AdversarialContent.token_list_contains_invalid_tokens(attack_state.tokenizer, adversarial_candidate.token_ids):
                    include_candidate = False
                    #print(f"[get_filtered_cands] Warning: adversarial_candidate '{adversarial_candidate.get_short_description()}' contains token ID {adversarial_candidate.token_ids[candidate_token_num]}, which is outside the valid range for this tokenizer (min = 0, max = {attack_state.tokenizer.vocab_size}). The candidate will be ignored. This may indicate an issue with the attack code, or the tokenizer code.")
            if include_candidate and filter_cand:
                include_candidate = False
                
                if not adversarial_candidate.is_match(attack_state.persistable.current_adversarial_content):
                    include_candidate = True
                else:
                    include_candidate = False
                    #print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate_message_represenation}' because it was equivalent to the current adversarial content value '{attack_state.persistable.current_adversarial_content.get_short_description()}'.")
                    filtered_due_to_already_being_tested.append(adversarial_candidate)
                if include_candidate:
                    if adversarial_candidate_message_represenation.strip() == "":
                        include_candidate = False
                        #print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate_message_represenation}' because it is an empty string, or equivalent to an empty string.")
                        filtered_due_to_empty_string.append(adversarial_candidate)
                if include_candidate:
                    if attack_state.persistable.tested_adversarial_content.contains_adversarial_content(adversarial_candidate):
                        include_candidate = False
                        #print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate_message_represenation}' because it was equivalent to a previous adversarial value.")
                        filtered_due_to_already_being_tested.append(adversarial_candidate)
                    #else:
                    #    print(f"[get_filtered_cands] Debug: candidate '{adversarial_candidate.get_short_description()}' is not equivalent to any previous adversarial values.")
                if include_candidate:
                    if include_candidate:
                        
                        candidate_token_count = len(adversarial_candidate.token_ids)
                        current_adversarial_content_token_count = len(attack_state.persistable.current_adversarial_content.token_ids)
                        if not isinstance(attack_state.persistable.attack_params.candidate_filter_tokens_min, type(None)):
                            if candidate_token_count < attack_state.persistable.attack_params.candidate_filter_tokens_min:
                                include_candidate = False
                                #print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate_message_represenation}' because its token count ({candidate_token_count}) was less than the minimum value specified ({attack_state.persistable.attack_params.candidate_filter_tokens_min}).")
                                filtered_due_to_insufficient_token_count.append(adversarial_candidate)
                        if not isinstance(attack_state.persistable.attack_params.candidate_filter_tokens_max, type(None)):
                            if candidate_token_count > attack_state.persistable.attack_params.candidate_filter_tokens_max:
                                include_candidate = False
                                #print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate_message_represenation}' because its token count ({candidate_token_count}) was greater than the maximum value specified ({attack_state.persistable.attack_params.candidate_filter_tokens_max}).")
                                filtered_due_to_excessive_token_count.append(adversarial_candidate)
                        if attack_state.persistable.attack_params.attempt_to_keep_token_count_consistent:
                            # Test whether or not the candidate can be decoded to a string, then re-encoded to token IDs without changing the number of tokens
                            # Note that this doesn't guarantee a lossless conversion. For example, if two tokens in the input become one token in the output, but a different token in the input becomes two tokens in the output, this check will still succeed.
                            reencoded_candidate_token_ids = encode_string_for_real_without_any_cowboy_funny_business(attack_state.tokenizer, adversarial_candidate.as_string)
                            #if candidate_token_count != current_adversarial_content_token_count:
                            if len(reencoded_candidate_token_ids) != candidate_token_count:
                                include_candidate = False
                                #print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate_message_represenation}' because its token count ({candidate_token_count}) was not equal to the length of '{attack_state.persistable.current_adversarial_content.get_short_description()}' ({current_adversarial_content_token_count}).")
                                filtered_due_to_nonmatching_token_count.append(adversarial_candidate)

                    if include_candidate:
                        if not isinstance(attack_state.persistable.attack_params.candidate_filter_newline_limit, type(None)):
                            newline_character_count = 0
                            for newline_character in ["\x0a", "\x0d"]:
                                if newline_character in adversarial_candidate.as_string:
                                    for current_char in adversarial_candidate.as_string:
                                        if current_char == newline_character:
                                            newline_character_count += 1
                            if newline_character_count > attack_state.persistable.attack_params.candidate_filter_newline_limit:
                                include_candidate = False
                                #print(f"[get_filtered_cands] Debug: '{adversarial_candidate_message_represenation}' rejected due to presence of newline character(s)")
                                filtered_due_to_containing_newline_characters.append(adversarial_candidate)
                        if include_candidate and filter_regex is not None:
                            if filter_regex.search(adversarial_candidate.as_string):
                                dummy = 1
                                #print(f"[get_filtered_cands] Debug: '{adversarial_candidate_message_represenation}' represented as '{adversarial_candidate.as_string}' passed the regular expression filter")
                            else:
                                include_candidate = False
                                print(f"[get_filtered_cands] Debug: rejecting candidate '{adversarial_candidate_message_represenation}' because '{adversarial_candidate.as_string}' failed to pass the regular expression filter '{attack_state.persistable.attack_params.candidate_filter_regex}'.")
                                filtered_due_to_not_matching_regex.append(adversarial_candidate)
                        if include_candidate and not isinstance(attack_state.persistable.attack_params.candidate_filter_repetitive_tokens, type(None)) and attack_state.persistable.attack_params.candidate_filter_repetitive_tokens > 0:
                            token_counts = {}
                            already_notified_tokens = []
                            for c_token in token_input_ids:
                                t_count = 1
                                if c_token in token_counts:
                                    t_count = token_counts[c_token] + 1
                                    if t_count >= attack_state.persistable.attack_params.candidate_filter_repetitive_tokens:
                                        include_candidate = False
                                        filtered_due_to_repetitive_tokens.append(adversarial_candidate)
                                        if c_token not in already_notified_tokens:
                                            already_notified_tokens.append(c_token)
                                            #print(f"[get_filtered_cands] Debug: '{adversarial_candidate_message_represenation}' rejected because it had more than {attack_state.persistable.attack_params.candidate_filter_repetitive_tokens} occurrences of the token '{c_token}'")
                                token_counts[c_token] = t_count
                            #if include_candidate:
                            #    print(f"[get_filtered_cands] Debug: '{adversarial_candidate_message_represenation}' passed the repetitive token filter.")
                        if include_candidate and not isinstance(attack_state.persistable.attack_params.candidate_filter_repetitive_lines, type(None)) and attack_state.persistable.attack_params.candidate_filter_repetitive_lines > 0:
                            candidate_lines = adversarial_candidate.as_string.splitlines()
                            token_counts = {}
                            already_notified_tokens = []
                            for c_line in candidate_lines:
                                t_count = 1
                                if c_line in token_counts:
                                    t_count = token_counts[c_line] + 1
                                    if t_count >= attack_state.persistable.attack_params.candidate_filter_repetitive_lines:
                                        include_candidate = False
                                        filtered_due_to_repetitive_lines.append(adversarial_candidate)
                                        if c_line not in already_notified_tokens:
                                            already_notified_tokens.append(c_line)
                                            #print(f"[get_filtered_cands] Debug: '{adversarial_candidate_message_represenation}' rejected because it had more than {attack_state.persistable.attack_params.candidate_filter_repetitive_lines} occurrences of the line '{c_line}'")
                                token_counts[c_line] = t_count
                            #if include_candidate:
                            #    print(f"[get_filtered_cands] Debug: '{adversarial_candidate_message_represenation}' passed the repetitive line filter.")
                            
                
            if include_candidate:
                if not isinstance(attack_state.persistable.attack_params.candidate_replace_newline_characters, type(None)):
                    decoded_str = adversarial_candidate.as_string
                    decoded_str = decoded_str.replace("\n", attack_state.persistable.attack_params.candidate_replace_newline_characters)
                    decoded_str = decoded_str.replace("\r", attack_state.persistable.attack_params.candidate_replace_newline_characters)
                    if decoded_str != adversarial_candidate.as_string:
                        adversarial_candidate = AdversarialContent.from_string(attack_state.tokenizer, attack_state.adversarial_content_manager.trash_fire_tokens, decoded_str)
                #print(f"[get_filtered_cands] Debug: appending '{adversarial_candidate_message_represenation}' to candidate list.\n")
                result.append_if_new(adversarial_candidate)
            else:
                #print(f"[get_filtered_cands] Debug: not appending '{adversarial_candidate_message_represenation}' to candidate list because it was filtered out.\n")
                filtered_count += 1

    #print(f"[get_filtered_cands] Debug: control_cand = {control_cand}, cands = {cands}")

    if filter_cand:
        if len(result.adversarial_content) == 0:
            dummy = 1
            #print(f"[get_filtered_cands] Warning: no candidates found")
        else:
            # I *think* this step is supposed to append copies of the last entry in the list enough times to make the new list as long as the original list
            #cands = cands + [cands[-1]] * (len(control_cand) - len(cands))
            # TKTK: try taking this out, because it seems weird to have to do this
            if len(result.adversarial_content) < len(new_adversarial_content_list.adversarial_content):
                while len(result.adversarial_content) < len(new_adversarial_content_list.adversarial_content):
                    result.adversarial_content.append(result.adversarial_content[-1].copy())
                    
            #print(f"[get_filtered_cands] Warning: {round(filtered_count / len(control_cand), 2)} control candidates were not valid")

    percent_passed = float(len(result.adversarial_content)) / float(len_new_adversarial_content_list)
    percent_rejected = float(filtered_count) / float(len_new_adversarial_content_list)
    if percent_rejected > attack_state.persistable.attack_params.warn_on_filtered_candidate_percentage:
        filter_warning = f"Warning: {len(result.adversarial_content)}/{len_new_adversarial_content_list} ({percent_rejected:.0%}) of adversarial value candidates were filtered out during this iteration, which is greater than the warning threshold of {attack_state.persistable.attack_params.warn_on_filtered_candidate_percentage:.0%}. This may be due to excessively strict or conflicting filtering options specified by the operator."
        len_filtered_due_to_empty_string = len(filtered_due_to_empty_string)
        if len_filtered_due_to_empty_string > 0:
            filter_warning = f"{filter_warning} {len_filtered_due_to_empty_string} candidate(s) were filtered out because they were equivalent to an empty string."

        len_filtered_due_to_already_being_tested = len(filtered_due_to_already_being_tested)
        if len_filtered_due_to_already_being_tested > 0:
            filter_warning = f"{filter_warning} {len_filtered_due_to_already_being_tested} candidate(s) were filtered out because they had already been tested in previous iterations."
        
        len_filtered_due_to_insufficient_token_count = len(filtered_due_to_insufficient_token_count)
        if len_filtered_due_to_insufficient_token_count > 0:
            filter_warning = f"{filter_warning} {len_filtered_due_to_insufficient_token_count} candidate(s) were filtered out because they had fewer than the minimum number of tokens specified by the operator ({attack_state.persistable.attack_params.candidate_filter_tokens_min})."

        len_filtered_due_to_excessive_token_count = len(filtered_due_to_excessive_token_count)
        if len_filtered_due_to_excessive_token_count > 0:
            filter_warning = f"{filter_warning} {len_filtered_due_to_excessive_token_count} candidate(s) were filtered out because they had more than the maximum number of tokens specified by the operator ({attack_state.persistable.attack_params.candidate_filter_tokens_max})."

        len_filtered_due_to_nonmatching_token_count = len(filtered_due_to_nonmatching_token_count)
        if len_filtered_due_to_nonmatching_token_count > 0:
            filter_warning = f"{filter_warning} {len_filtered_due_to_nonmatching_token_count} candidate(s) were filtered out because they had a different number of tokens than the current adversarial value, and the option to keep token count consistent is enabled."

        len_filtered_due_to_containing_newline_characters = len(filtered_due_to_containing_newline_characters)
        if len_filtered_due_to_containing_newline_characters > 0:
            filter_warning = f"{filter_warning} {len_filtered_due_to_containing_newline_characters} candidate(s) were filtered out because they contained more than the number of allowed newline characters."

        len_filtered_due_to_not_matching_regex = len(filtered_due_to_not_matching_regex)
        if len_filtered_due_to_not_matching_regex > 0:
            filter_warning = f"{filter_warning} {len_filtered_due_to_not_matching_regex} candidate(s) were filtered out because they did not match the regular expression '{attack_state.persistable.attack_params.candidate_filter_regex}'."

        len_filtered_due_to_repetitive_tokens = len(filtered_due_to_repetitive_tokens)
        if len_filtered_due_to_repetitive_tokens > 0:
            filter_warning = f"{filter_warning} {len_filtered_due_to_repetitive_tokens} candidate(s) were filtered out because they had had more than the operator-specified number of repetitive tokens ({attack_state.persistable.attack_params.candidate_filter_repetitive_tokens})."

        len_filtered_due_to_repetitive_lines = len(filtered_due_to_repetitive_lines)
        if len_filtered_due_to_repetitive_lines > 0:
            filter_warning = f"{filter_warning} {len_filtered_due_to_repetitive_lines} candidate(s) were filtered out because they had had more than the operator-specified number of repetitive lines ({attack_state.persistable.attack_params.candidate_filter_repetitive_lines})."
        
        print(filter_warning)

    return result

def get_logits(attack_state, input_ids, adversarial_content, adversarial_candidate_list = None, return_ids = False):
    
    if adversarial_candidate_list is None or len(adversarial_candidate_list.adversarial_content) < 1:
        raise ValueError(f"adversarial_candidate_list must be an AdversarialContentList with at least 1 entry. Got empty array or null.")

    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_logits - begin")

    test_ids = None
    nested_ids = None

    number_of_adversarial_token_ids = len(adversarial_content.token_ids)

    max_len = number_of_adversarial_token_ids
    test_ids = []
    for i in range(0, len(adversarial_candidate_list.adversarial_content)):        
        tid = torch.tensor(adversarial_candidate_list.adversarial_content[i].token_ids[:max_len], device = attack_state.model.device)
        test_ids.append(tid)

    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_logits - after populating test_ids")

    pad_tok = 0
    while pad_tok in input_ids or any([pad_tok in ids for ids in test_ids]):
        pad_tok += 1
    nested_ids = torch.nested.nested_tensor(test_ids)
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_logits - after creating nested_ids")
    test_ids = torch.nested.to_padded_tensor(nested_ids, pad_tok, (len(test_ids), max_len))
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_logits - after recreating nested_ids")

    decoded_test_ids = get_decoded_tokens(attack_state.tokenizer, test_ids)
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_logits - after creating decoded_test_ids")
    #print(f"[get_logits] Debug: test_ids = '{test_ids}'\n decoded_test_ids = '{decoded_test_ids}'")

    if not(test_ids[0].shape[0] == number_of_adversarial_token_ids):
        raise ValueError((
            f"adversarial_candidate_list must have shape "
            f"(n, {number_of_adversarial_token_ids}), " 
            f"got {test_ids.shape}"
        ))

    locs = torch.arange(0, number_of_adversarial_token_ids).repeat(test_ids.shape[0], 1).to(attack_state.model.device)
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_logits - after creating locs")
    ids = torch.scatter(
        input_ids.unsqueeze(0).repeat(test_ids.shape[0], 1).to(attack_state.model.device),
        1,
        locs,
        test_ids
    )
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_logits - after creating ids")
    del locs
    del test_ids
    gc.collect()
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_logits - after deleting locs and test_ids and running garbage collection")
    attn_mask = None
    if pad_tok >= 0:
        attn_mask = (ids != pad_tok).type(ids.dtype)
    else:
        attn_mask = None

    if return_ids:
        #del locs, test_ids ; gc.collect()
        result1 = forward(attack_state = attack_state, input_ids = ids, attention_mask = attn_mask, batch_size = attack_state.persistable.attack_params.batch_size_get_logits)
        #print(f"[get_logits] Debug: returning result1 = '{result1}', ids = '{ids}', attn_mask = '{attn_mask}'")
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_logits - after creating result1 via forward")
        return result1, ids
    else:
        #del locs, test_ids
        logits = forward(attack_state = attack_state, input_ids = ids, attention_mask = attn_mask, batch_size = attack_state.persistable.attack_params.batch_size_get_logits)
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_logits - after creating logits via forward")
        del ids
        gc.collect()
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "get_logits - after deleting ids and running garbage collection")
        
        #print(f"[get_logits] Debug: returning logits = '{logits}', attn_mask = '{attn_mask}'")
        
        return logits
    

def forward(*, attack_state, input_ids, attention_mask, batch_size = 512):

    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "forward - begin")

    logits = []
    # completing this loop results in PyTorch reserving about 4.3 GiB of additional device memory for a 500M parameter model with new_row = model_result.logits.detach().clone() and batch_size = 512
    # By my math, it should be using more like 852 MB if the values are 16-bit floats
    # [forward] Debug: new_row.shape = torch.Size([46, 61, 151936]), len(logits) = 1
    # With batch_size = 1, no increase for 15 iterations, then it eventually works its way up to about 3 GiB of additional device memory
    # With batch_size = 1, model_result.logits, it's about 1.9 GiB instead of ~3 GiB of additional device memory, but jumps to about 3.6 GiB after the torch.cat operation
    for i in range(0, input_ids.shape[0], batch_size):
        
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = f"forward - beginning of loop iteration {i + 1}")
        
        batch_input_ids = input_ids[i:i+batch_size]
        batch_attention_mask = None
        if attention_mask is not None:
            batch_attention_mask = attention_mask[i:i+batch_size]
        else:
            batch_attention_mask = None

        model_result = None
        if attack_state.persistable.attack_params.use_attention_mask:
            model_result = attack_state.model(input_ids = batch_input_ids, attention_mask = batch_attention_mask)
        else:
            model_result = attack_state.model(input_ids = batch_input_ids)
        #model_result_decoded = get_decoded_tokens(attack_state.tokenizer, model_result)
        #print(f"[forward] Debug: getting logits for model_result = '{model_result}', model_result_decoded = '{model_result_decoded}'")
        #print(f"[forward] Debug: getting logits for model_result = '{model_result}'")

        logits.append(model_result.logits)
        print(f"[forward] Debug: new_row.shape = {model_result.logits.shape}, len(logits) = {len(logits)}")
        #new_row = model_result.logits.detach().clone()
        #logits.append(new_row)
        #print(f"[forward] Debug: new_row.shape = {new_row.shape}, len(logits) = {len(logits)}")

        del model_result
        del batch_input_ids
        del batch_attention_mask
        gc.collect()

    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "forward - after loop")
    result = torch.cat(logits, dim=0)
    del logits
    gc.collect()
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "forward - after creating result")    
    return result

# In this function, the logits returned by get_logits and forward are compared against the token IDs returned by get_logits
# ...which seems to correspond to the token IDs that represent the target output, repeated enough times to equal the length of the first entry in the list of candidate values? I think?
def target_loss(attack_state, logits, ids, input_id_data):
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "target_loss - begin")
    logits_sliced = logits[:,input_id_data.slice_data.loss,:]
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "target_loss - after creating logits_sliced")
    logits_sliced_transposed = logits_sliced.transpose(1,2)
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "target_loss - after creating logits_sliced_transposed")
    ids_sliced = ids[:,input_id_data.slice_data.target_output]
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "target_loss - after creating ids_sliced")
    
    ids_sliced = get_padded_target_token_ids(attack_state.tokenizer, input_id_data.slice_data.loss, ids_sliced)
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "target_loss - after recreating ids_sliced")
    
    # comment/uncomment the next two lines together
    #ids_sliced_decoded = get_decoded_tokens(attack_state.tokenizer, ids_sliced)
    #print(f"[target_loss] Debug: calculating loss. logits_sliced = '{logits_sliced}', logits_sliced_transposed = '{logits_sliced_transposed}', ids_sliced = '{ids_sliced}', ids_sliced_decoded = '{ids_sliced_decoded}'")

    got_loss = False
    loss_logits = logits[0,input_id_data.slice_data.loss,:]
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "target_loss - after creating loss_logits")
    if attack_state.persistable.attack_params.loss_algorithm == LossAlgorithm.CROSS_ENTROPY:
        crit = nn.CrossEntropyLoss(reduction='none')
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "target_loss - after creating crit")
        loss = crit(logits_sliced_transposed, ids_sliced)
        attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "target_loss - after creating loss")
        got_loss = True
    # if not got_loss and attack_state.persistable.attack_params.loss_algorithm == LossAlgorithm.MELLOWMAX:
        # #loss = mellowmax(-loss_logits, alpha = attack_state.persistable.attack_params.mellowmax_alpha, dim = -1)
        # #crit = nn.CrossEntropyLoss(reduction='none')
        # #loss = crit(logits_sliced_transposed, ids_sliced)
        # #print(f"[target_loss] Debug: cross-entropy loss = '{loss}'")
        
        # loss = mellowmax(-loss_logits, alpha = attack_state.persistable.attack_params.mellowmax_alpha)
        # #print(f"[target_loss] Debug: mellowmax loss = '{loss}'")
        
        # got_loss = True
    if not got_loss:
        print("[target_loss] Error: unknown loss algorithm '{attack_state.persistable.attack_params.loss_algorithm}'")
        sys.exit(1)

    #print(f"[target_loss] Debug: loss = '{loss}'")

    result = loss.mean(dim=-1)
    attack_state.persistable.performance_data.collect_torch_stats(attack_state, location_description = "target_loss - after creating result")

    #print(f"[target_loss] Debug: result = '{result}'")

    return result

def get_missing_pad_token_names():
    result = [  "unk", 
                "bos",
                "eos" ]
    return result

def get_missing_pad_token_replacement(tokenizer, replacement_name):
    allowed_names = get_missing_pad_token_names()
    if replacement_name not in get_missing_pad_token_names():
        raise Exception(f"Unrecognized padding token replacement name: '{replacement_name}' - must be one of '{allowed_names}'")
    result = None
    if replacement_name == "bos":
        result = tokenizer.bos_token_id, tokenizer.bos_token
    if replacement_name == "eos":
        result = tokenizer.eos_token_id, tokenizer.eos_token
    if replacement_name == "unk":
        result = tokenizer.unk_token_id, tokenizer.unk_token
    return result

def load_model_and_tokenizer(attack_state):
    #print(f"[load_model_and_tokenizer] Debug: attack_state.persistable.attack_params.model_path = '{attack_state.persistable.attack_params.model_path}', attack_state.persistable.attack_params.tokenizer_path = '{attack_state.persistable.attack_params.tokenizer_path}', device = '{device}', dtype = {dtype}, trust_remote_code = {trust_remote_code}, ignore_mismatched_sizes = {ignore_mismatched_sizes}")

    #if ignore_mismatched_sizes:
    #    kwargs["ignore_mismatched_sizes"] = True

    # Hey, everyone, I've got a great idea! I'll use a machine-learning library with a full-featured list of data types, like int8, float16, bfloat16, and float32. It has a model-loading function that accepts one of those data types if the user wants to force conversion to that type. But I'll randomly decide to make the library default to converting to my personal favourite type when it loads my model! And I'll also invent a completely separate way of representing the data types for the option to override my favourite type, instead of using the full-featured list that's already there! Pew pew! Look at me! I'm Charlie Prince!
    # Inspired by the following PyTorch output:
    #   The model is automatically converting to bf16 for faster inference. If you want to disable the automatic precision, please manually add bf16/fp16/fp32=True to "AutoModelForCausalLM.from_pretrained".
    #   https://huggingface.co/Qwen/Qwen-7B/commit/58362a19a5b5b41c88ed1ae04607d733e1df4944

    model = None

    if attack_state.model_weight_type is None:
        model = AutoModelForCausalLM.from_pretrained(
                attack_state.persistable.attack_params.model_path,
                trust_remote_code = attack_state.persistable.attack_params.load_options_trust_remote_code,
                ignore_mismatched_sizes = attack_state.persistable.attack_params.load_options_ignore_mismatched_sizes,
                low_cpu_mem_usage = attack_state.persistable.attack_params.low_cpu_mem_usage,
                use_cache = attack_state.persistable.attack_params.use_cache
            ).to(attack_state.model_device).eval()
    else:
        # because we don't have a config yet to call hasattr against, seems like we have to try calling the next function with the specific parameters first, catch an exception, and try again without them
        charlie_prince_bf16 = False
        charlie_prince_fp16 = False
        charlie_prince_fp32 = False
        if attack_state.model_weight_type == torch.bfloat16:
            charlie_prince_bf16 = True
        if attack_state.model_weight_type == torch.float16:
            charlie_prince_fp16 = True
        if attack_state.model_weight_type == torch.float32:
            charlie_prince_fp32= True
        #print(f"[load_model_and_tokenizer] Debug: attack_state.model_weight_type = {attack_state.model_weight_type}, charlie_prince_bf16 = {charlie_prince_bf16}, charlie_prince_fp16 = {charlie_prince_fp16}, charlie_prince_fp32 = {charlie_prince_fp32}")
        try:
            model = AutoModelForCausalLM.from_pretrained(
                    attack_state.persistable.attack_params.model_path,
                    torch_dtype = attack_state.model_weight_type,
                    bf16 = charlie_prince_bf16,
                    fp16 = charlie_prince_fp16,
                    fp32 = charlie_prince_fp32,
                    trust_remote_code = attack_state.persistable.attack_params.load_options_trust_remote_code,
                    ignore_mismatched_sizes = attack_state.persistable.attack_params.load_options_ignore_mismatched_sizes,
                    low_cpu_mem_usage = attack_state.persistable.attack_params.low_cpu_mem_usage,
                    use_cache = attack_state.persistable.attack_params.use_cache
                ).to(attack_state.model_device).eval()
        except Exception as e:
            #print(f"[load_model_and_tokenizer] Debug: Exception thrown when loading model with notorious outlaw Charlie Prince's personal custom parameters: {e}")
            model = AutoModelForCausalLM.from_pretrained(
                    attack_state.persistable.attack_params.model_path,
                    torch_dtype = attack_state.model_weight_type,
                    trust_remote_code = attack_state.persistable.attack_params.load_options_trust_remote_code,
                    ignore_mismatched_sizes = attack_state.persistable.attack_params.load_options_ignore_mismatched_sizes,
                    low_cpu_mem_usage = attack_state.persistable.attack_params.low_cpu_mem_usage,
                    use_cache = attack_state.persistable.attack_params.use_cache
                ).to(attack_state.model_device).eval()                
    
    tokenizer_path_to_load = attack_state.persistable.attack_params.model_path
    if attack_state.persistable.attack_params.tokenizer_path is not None:
        tokenizer_path_to_load = attack_state.persistable.attack_params.tokenizer_path
    
    #print(f"[load_model_and_tokenizer] Debug: attack_state.persistable.attack_params.tokenizer_path = '{attack_state.persistable.attack_params.tokenizer_path}', attack_state.persistable.attack_params.model_path = '{attack_state.persistable.attack_params.model_path}'")

    tokenizer = None
    
    #is_mamba = args.model_name.startswith("state-spaces/mamba-")
    #    if is_mamba:
    #tokenizer = AutoTokenizer.from_pretrained("EleutherAI/gpt-neox-20b")
    #model = MambaLMHeadModel.from_pretrained(args.model_name, device = attack_state.model_device, attack_state.model_weight_type = attack_state.model_weight_type)
    
    try:
        tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_path_to_load,
            trust_remote_code = attack_state.persistable.attack_params.load_options_trust_remote_code,
            use_fast = False
        )
    except Exception as e:
        handled = False
        #if isinstance(e, ValueError):
        if 2 > 1:
            print(f"[load_model_and_tokenizer] Warning: unable to load standard tokenizer from '{tokenizer_path_to_load}', attempting to fall back to fast tokenizer. The exception thrown when loading the standard tokenizer was: {e}")
            try:
                tokenizer = AutoTokenizer.from_pretrained(
                    tokenizer_path_to_load,
                    trust_remote_code = attack_state.persistable.attack_params.load_options_trust_remote_code,
                    use_fast = True
                )
                handled = True
            except Exception as e2:
                print(f"[load_model_and_tokenizer] Error loading both standard and fast tokenizers from '{tokenizer_path_to_load}': '{e}', '{e2}'")
                raise e        
        if not handled:
            print(f"[load_model_and_tokenizer] Error loading tokenizer from '{tokenizer_path_to_load}': '{e}'")
            raise e
    
    if attack_state.persistable.attack_params.enable_hardcoded_tokenizer_workarounds:
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
        if attack_state.persistable.attack_params.missing_pad_token_replacement is not None:
            tokenizer.pad_token_id, tokenizer.pad_token = get_missing_pad_token_replacement(tokenizer, attack_state.persistable.attack_params.missing_pad_token_replacement)
            print(f"[load_model_and_tokenizer] Warning: the tokenizer in '{tokenizer_path_to_load}' does not have a pad_token value defined. Using the alternative value '{attack_state.persistable.attack_params.missing_pad_token_replacement}' specified by the operator. If you encounter errors or unexpected results, consider specifying a different --missing-pad-token-replacement value on the command line.")
        else:
            print(f"[load_model_and_tokenizer] Warning: the tokenizer in '{tokenizer_path_to_load}' does not have a pad_token value defined. If you encounter errors or unexpected results, consider specifying a --missing-pad-token-replacement value on the command line.")
    
    return model, tokenizer
