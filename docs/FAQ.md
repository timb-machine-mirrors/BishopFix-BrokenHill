# Frequently-asked questions

## Can I run this using something other than an Nvidia GPU?

You can use the PyTorch `cpu` device if you really want to, but it will literally take hundreds of times longer to run, e.g. 2+ hours for a single iteration of the main loop.

[The `mps` (Metal for Mac OS) PyTorch back-end does not support some of the necessary features](https://github.com/pytorch/pytorch/issues/127743) ([e.g. nested tensors](https://github.com/pytorch/pytorch/blob/3855ac5a5d53fd4d2d6521744eaf80c2a95a4d54/aten/src/ATen/NestedTensorImpl.cpp#L183) yet, so using `--device mps` will cause the script to crash not long after loading the model.

## Can I quantize the models to use less VRAM?

No (for any type of integer quantization, which is the main type of quantization that inspires this question). The source code for this modified version is littered with my grumpy comments related to lack of support for quantized integer values in PyTorch for some of the features this tool depends upon.

In the unlikely event that you are asking about the corner case where you have a model with weights in a larger format than 16-bit floating point that hasn't already also been released in a 16-bit floating point format, you could quantize that model to a 16-bit floating point format, and it will occupy less VRAM when you load it into the tool. Most publicly-available models that you'd want to use with this tool are already available in such a format.