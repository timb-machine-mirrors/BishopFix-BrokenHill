#!/bin/env python3

import json
import os
import torch

from enum import IntFlag
#from enum import StrEnum
from enum import auto
from llm_attacks_bishopfox.json_serializable_object import JSONSerializableObject
from llm_attacks_bishopfox.util.util_functions import add_value_to_list_if_not_already_present
from llm_attacks_bishopfox.util.util_functions import add_values_to_list_if_not_already_present
from llm_attacks_bishopfox.util.util_functions import get_file_content
from llm_attacks_bishopfox.util.util_functions import torch_dtype_from_string

BUNDLED_LLM_LIST_FILE_NAME = "model_list.json"

class LargeLanguageModelException(Exception):
    pass

class BrokenHillModelSupportState(IntFlag):
    # Is the Torch configuration class for the model supported?
    # e.g. GemmaConfig, Phi3Config
    TORCH_CONFIGURATION_CLASS_SUPPORTED = auto()
    # Can the model be run for at least two iterations in Broken Hill without crashing and generating valid output?
    PASSES_SMOKE_TEST = auto()
    # Is there a chat template for the model that works reasonably well?
    HAS_KNOWN_CHAT_TEMPLATE = auto()

def model_support_state_to_list(model_support_state):
    result = []
    if (model_support_state & BrokenHillModelSupportState.TORCH_CONFIGURATION_CLASS_SUPPORTED) == BrokenHillModelSupportState.TORCH_CONFIGURATION_CLASS_SUPPORTED:
        result.append(str(BrokenHillModelSupportState.TORCH_CONFIGURATION_CLASS_SUPPORTED))
    if (model_support_state & BrokenHillModelSupportState.PASSES_SMOKE_TEST) == BrokenHillModelSupportState.PASSES_SMOKE_TEST:
        result.append(str(BrokenHillModelSupportState.PASSES_SMOKE_TEST))
    if (model_support_state & BrokenHillModelSupportState.TORCH_CONFIGURATION_CLASS_SUPPORTED) == BrokenHillModelSupportState.TORCH_CONFIGURATION_CLASS_SUPPORTED:
        result.append(str(BrokenHillModelSupportState.TORCH_CONFIGURATION_CLASS_SUPPORTED))
    return result

def model_support_state_from_list(model_support_state_flag_list):
    result = BrokenHillModelSupportState()
    if str(BrokenHillModelSupportState.TORCH_CONFIGURATION_CLASS_SUPPORTED) in model_support_state_flag_list:
        result = result | BrokenHillModelSupportState.TORCH_CONFIGURATION_CLASS_SUPPORTED
    if str(BrokenHillModelSupportState.PASSES_SMOKE_TEST) in model_support_state_flag_list:
        result = result | BrokenHillModelSupportState.PASSES_SMOKE_TEST
    if str(BrokenHillModelSupportState.HAS_KNOWN_CHAT_TEMPLATE) in model_support_state_flag_list:
        result = result | BrokenHillModelSupportState.HAS_KNOWN_CHAT_TEMPLATE
    return result

class BrokenHillModelAlignmentInfo(IntFlag):
    # Does the model have one or more alignment/trained restrictions against generating certain types of output?
    MODEL_HAS_ALIGNMENT_RESTRICTIONS = auto()
    # Has bypass of at least one type of alignment/trained restriction been demonstrated using Broken Hill?
    BROKEN_HILL_HAS_DEFEATED_ALIGNMENT = auto()
    # Does the model generally follow instructions in the system prompt or template messages regarding generation of content?
    MODEL_GENERALLY_FOLLOWS_ADDITIONAL_RESTRICTIONS = auto()
    # Has bypass of system prompt/template message instructions been demonstrated using Broken Hill?
    BROKEN_HILL_HAS_DEFEATED_ADDITIONAL_RESTRICTIONS = auto()

def alignment_info_to_list(model_support_state):
    result = []
    if (model_support_state & BrokenHillModelAlignmentInfo.MODEL_HAS_ALIGNMENT_RESTRICTIONS) == BrokenHillModelAlignmentInfo.MODEL_HAS_ALIGNMENT_RESTRICTIONS:
        result.append(str(BrokenHillModelAlignmentInfo.MODEL_HAS_ALIGNMENT_RESTRICTIONS))
    if (model_support_state & BrokenHillModelAlignmentInfo.BROKEN_HILL_HAS_DEFEATED_ALIGNMENT) == BrokenHillModelAlignmentInfo.BROKEN_HILL_HAS_DEFEATED_ALIGNMENT:
        result.append(str(BrokenHillModelAlignmentInfo.BROKEN_HILL_HAS_DEFEATED_ALIGNMENT))
    if (model_support_state & BrokenHillModelAlignmentInfo.MODEL_GENERALLY_FOLLOWS_ADDITIONAL_RESTRICTIONS) == BrokenHillModelAlignmentInfo.MODEL_GENERALLY_FOLLOWS_ADDITIONAL_RESTRICTIONS:
        result.append(str(BrokenHillModelAlignmentInfo.MODEL_GENERALLY_FOLLOWS_ADDITIONAL_RESTRICTIONS))
    if (model_support_state & BrokenHillModelAlignmentInfo.BROKEN_HILL_HAS_DEFEATED_ADDITIONAL_RESTRICTIONS) == BrokenHillModelAlignmentInfo.BROKEN_HILL_HAS_DEFEATED_ADDITIONAL_RESTRICTIONS:
        result.append(str(BrokenHillModelAlignmentInfo.BROKEN_HILL_HAS_DEFEATED_ADDITIONAL_RESTRICTIONS))
    return result

def alignment_info_from_list(model_support_state_flag_list):
    result = BrokenHillModelAlignmentInfo()
    if str(BrokenHillModelAlignmentInfo.MODEL_HAS_ALIGNMENT_RESTRICTIONS) in model_support_state_flag_list:
        result = result | BrokenHillModelAlignmentInfo.MODEL_HAS_ALIGNMENT_RESTRICTIONS
    if str(BrokenHillModelAlignmentInfo.BROKEN_HILL_HAS_DEFEATED_ALIGNMENT) in model_support_state_flag_list:
        result = result | BrokenHillModelAlignmentInfo.BROKEN_HILL_HAS_DEFEATED_ALIGNMENT
    if str(BrokenHillModelAlignmentInfo.MODEL_GENERALLY_FOLLOWS_ADDITIONAL_RESTRICTIONS) in model_support_state_flag_list:
        result = result | BrokenHillModelAlignmentInfo.MODEL_GENERALLY_FOLLOWS_ADDITIONAL_RESTRICTIONS
    if str(BrokenHillModelAlignmentInfo.BROKEN_HILL_HAS_DEFEATED_ADDITIONAL_RESTRICTIONS) in model_support_state_flag_list:
        result = result | BrokenHillModelAlignmentInfo.BROKEN_HILL_HAS_DEFEATED_ADDITIONAL_RESTRICTIONS
    return result

class LargeLanguageModelParameterInfo(JSONSerializableObject):
    def __init__(self):
        self.module_name = None
        self.parameter_count = None
        self.is_trainable = None

    def to_dict(self):
        result = super(LargeLanguageModelParameterInfo, self).properties_to_dict(self)
        return result
    
    @staticmethod
    def from_dict(property_dict):
        result = LargeLanguageModelParameterInfo()
        super(LargeLanguageModelParameterInfo, result).set_properties_from_dict(result, property_dict)
        return result

    def to_json(self):
        return JSONSerializableObject.json_dumps(self.to_dict(), use_indent = False)
        
    @staticmethod
    def from_json(json_string):
        return LargeLanguageModelParameterInfo.from_dict(json.loads(json_string))
    
    def copy(self):
        result = LargeLanguageModelParameterInfo()
        return LargeLanguageModelParameterInfo.set_properties_from_dict(result, self.to_dict())

class LargeLanguageModelParameterInfoCollection(JSONSerializableObject):
    def __init__(self):
        self.parameters = {}
        self.total_parameter_count = None
        self.trainable_parameter_count = None
        self.nontrainable_parameter_count = None

    def get_total_parameter_count(only_trainable = False):
        result = 0
        for param_name in self.parameters.keys():
            param = self.parameters[param_name]
            if only_trainable:
                if not param.is_trainable:
                    continue
            result += param.parameter_count
        return result
    
    def set_parameter_counts(self):
        self.total_parameter_count = self.get_total_parameter_count(only_trainable = False)
        self.trainable_parameter_count = self.get_total_parameter_count(only_trainable = True)
        self.nontrainable_parameter_count = self.total_parameter_count - self.trainable_parameter_count
    
    # BEGIN: based in part on https://stackoverflow.com/questions/49201236/check-the-total-number-of-parameters-in-a-pytorch-model
    @staticmethod
    def get_model_parameter_info(model):
        result = {}
        for name, parameter in model.named_parameters():
            param_info = LargeLanguageModelParameterInfo()
            param_info.module_name = name
            param_info.is_trainable = parameter.requires_grad
            param_info.parameter_count = parameter.numel()
            result[name] = param_info
        return result
    # END: based in part on https://stackoverflow.com/questions/49201236/check-the-total-number-of-parameters-in-a-pytorch-model

    @staticmethod
    def from_loaded_model(model):
        result = LargeLanguageModelParameterInfoCollection()
        result.parameters = LargeLanguageModelParameterInfoCollection.get_model_parameter_info(model)
        result.set_parameter_counts()        
        return result

    def to_dict(self):
        result = super(LargeLanguageModelParameterInfoCollection, self).properties_to_dict(self)
        return result
    
    @staticmethod
    def from_dict(property_dict):
        result = LargeLanguageModelParameterInfoCollection()
        super(LargeLanguageModelParameterInfoCollection, result).set_properties_from_dict(result, property_dict)
        if result.parameters is not None:
            if len(result.parameters) > 0:
                deserialized_content = []
                for i in range(0, len(result.parameters)):
                    deserialized_content.append(LargeLanguageModelParameterInfo.from_dict(result.parameters[i]))
                result.parameters = deserialized_content
        return result

    def to_json(self):
        return JSONSerializableObject.json_dumps(self.to_dict(), use_indent = False)
        
    @staticmethod
    def from_json(json_string):
        return LargeLanguageModelParameterInfoCollection.from_dict(json.loads(json_string))
    
    def copy(self):
        result = LargeLanguageModelParameterInfoCollection()
        return LargeLanguageModelParameterInfoCollection.set_properties_from_dict(result, self.to_dict())

    
class LargeLanguageModelInfo(JSONSerializableObject):
    def __init__(self):
        self.model_name = None
        self.model_release = None
        self.model_family = None
        self.model_repository = None
        self.direct_developer_or_publisher = False
        self.model_path = None
        self.tokenizer_path = None
        self.peft_path = None
        self.template = None
        self.size = None
        self.data_type = None
        self.parameter_count = None
        self.parameter_info_collection = None
        self.safe_tensors = None
        self.custom_options = None
        self.support_state = BrokenHillModelSupportState()
        self.alignment_info = BrokenHillModelAlignmentInfo()
        self.comment = None    
    
    def get_parameter_count(self):
        if self.parameter_count is not None:
            return self.parameter_count
        if self.parameter_info_collection is None:
            return None        
        return self.parameter_info_collection.total_parameter_count
    
    def to_dict(self):
        result = super(LargeLanguageModelInfo, self).properties_to_dict(self)
        result["support_state"] = model_support_state_to_list(self.support_state)
        result["alignment_info"] = alignment_info_to_list(self.alignment_info)
        return result
    
    @staticmethod
    def from_dict(property_dict):
        result = LargeLanguageModelInfo()
        super(LargeLanguageModelInfo, result).set_properties_from_dict(result, property_dict)
        if result.parameter_info_collection is not None:
            result.parameter_info_collection = LargeLanguageModelParameterInfoCollection.from_dict(result.parameter_info_collection)
        if result.support_state is not None:
            result.support_state = model_support_state_from_list(result.support_state)
        if result.alignment_info is not None:
            result.alignment_info = alignment_info_from_list(result.alignment_info)
        return result

    def to_json(self):
        return JSONSerializableObject.json_dumps(self.to_dict(), use_indent = False)
        
    @staticmethod
    def from_json(json_string):
        return LargeLanguageModelInfo.from_dict(json.loads(json_string))
    
    def copy(self):
        result = LargeLanguageModelInfo()
        return LargeLanguageModelInfo.set_properties_from_dict(result, self.to_dict())

class LargeLanguageModelInfoList(JSONSerializableObject):
    def __init__(self):
        self.entries = []
    
    def to_dict(self):
        result = super(LargeLanguageModelInfoList, self).properties_to_dict(self)
        return result
    
    @staticmethod
    def from_dict(property_dict):
        result = LargeLanguageModelInfoList()
        super(LargeLanguageModelInfoList, result).set_properties_from_dict(result, property_dict)
        if len(result.entries) > 0:
            deserialized_content = []
            for i in range(0, len(result.entries)):
                deserialized_content.append(LargeLanguageModelInfo.from_dict(result.entries[i]))
            result.entries = deserialized_content
        return result

    def to_json(self):
        return JSONSerializableObject.json_dumps(self.to_dict(), use_indent = False)
        
    @staticmethod
    def from_json(json_string):
        return LargeLanguageModelInfoList.from_dict(json.loads(json_string))
    
    def copy(self):
        result = LargeLanguageModelInfoList()
        return LargeLanguageModelInfoList.set_properties_from_dict(result, self.to_dict())
    
    @staticmethod
    def from_bundled_json_file():
        base_path = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_path, BUNDLED_LLM_LIST_FILE_NAME)
        file_content = get_file_content(file_path, failure_is_critical = True)
        if file_content is None:
            raise LargeLanguageModelException(f"Found no content in the file '{file_path}'")
        return LargeLanguageModelInfoList.from_json(file_content)
