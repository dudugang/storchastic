import storch
import torch
from collections.abc import Iterable
# from storch.tensor import Tensor, DeterministicTensor, StochasticTensor

_context_stochastic = False
_context_deterministic = False
_stochastic_parents = []
_plate_links = []

def _unwrap(*args, **kwargs):
    parents = []
    plates: [storch.StochasticTensor] = []

    # Collect parent tensors and plates
    for a in args:
        if isinstance(a, storch.Tensor):
            parents.append(a)
            for plate in a.batch_links:
                print("??????")
                if plate not in plates:
                    plates.append(plate)

    if len(kwargs.values()) > 0:
        print("unchecked kwargs pass")
        # raise NotImplementedError("Unwrapping of kwargs is currently not supported")

    print("here")
    storch.wrappers._plate_links = plates

    # Unsqueeze and align batched dimensions so that batching works easily.
    unsqueezed = []
    for t in args:
        if not isinstance(t, storch.Tensor):
            unsqueezed.append(t)
            print("unchecked argument pass")
            continue
        tensor = t._tensor

        # It can be possible that the ordering of the plates does not align with the ordering of the inputs.
        # This part corrects this.
        amt_recognized = 0
        links = t.batch_links.copy()
        for i, plate in enumerate(plates):
            if plate in t.batch_links:
                if plate is not links[amt_recognized]:
                    # The plate is also in the tensor, but not in the ordering expected. So switch that ordering
                    j = links.index(plate)
                    tensor = tensor.transpose(j, amt_recognized)
                    links[amt_recognized], links[j] = links[j], links[amt_recognized]
                amt_recognized += 1

        for i, plate in enumerate(plates):
            if plate not in t.batch_links:
                tensor = tensor.unsqueeze(i)
        unsqueezed.append(tensor)

    return tuple(unsqueezed), parents, plates


def _process_deterministic(o, parents, plates, is_cost):
    if isinstance(o, storch.Tensor):
        raise RuntimeError("Creation of storch Tensor within deterministic context")
    if isinstance(o, torch.Tensor):
        t = storch.DeterministicTensor(o, parents, plates, is_cost)
        if is_cost and t.event_shape != ():
            # TODO: Make sure the o.size() check takes into account the size of the sample.
            raise ValueError("Event shapes (ie, non batched dimensions) of cost nodes have to be single floating point numbers. ")
        return t
    raise NotImplementedError("Handling of other types of return values is currently not implemented: ", o)


def _deterministic(fn, is_cost):
    def wrapper(*args, **kwargs):
        print("start of wrapper", fn)
        if storch.wrappers._context_stochastic:
            # TODO check if we can re-add this
            raise NotImplementedError("It is currently not allowed to open a deterministic context in a stochastic context")
        if storch.wrappers._context_deterministic:
            if is_cost:
                raise RuntimeError("Cannot call storch.cost from within a deterministic context.")

            # We are already in a deterministic context, no need to wrap or unwrap as only the outer dependencies matter
            return fn(*args, **kwargs)
        print("Here3")
        args, parents, plates = _unwrap(*args, **kwargs)
        print("Here4")

        if not parents:
            return fn(*args, **kwargs)
        storch.wrappers._context_deterministic = True
        outputs = fn(*args, **kwargs)
        if isinstance(outputs, Iterable) and not isinstance(outputs, torch.Tensor):
            n_outputs = []
            for o in outputs:
                n_outputs.append(_process_deterministic(o, parents, plates, is_cost))
            outputs = n_outputs
        else:
            outputs = _process_deterministic(outputs, parents, plates, is_cost)
        storch.wrappers._context_deterministic = False
        return outputs
    return wrapper


def deterministic(fn):
    return _deterministic(fn, False)


def cost(fn):
    return _deterministic(fn, True)


def _process_stochastic(output, parents, plates):
    if isinstance(output, storch.Tensor):
        if not output.stochastic:
            # TODO: Calls _add_parents so something is going wrong here
            # The Tensor was created by calling @deterministic within a stochastic context.
            # This means that we have to conservatively assume it is dependent on the parents
            output._add_parents(storch.wrappers._stochastic_parents)
        return output
    if isinstance(output, torch.Tensor):
        t = storch.DeterministicTensor(output, parents, plates, False)
        return t
    else:
        raise TypeError("All outputs of functions wrapped in @storch.stochastic "
                        "should be Tensors. At " + str(output))


def stochastic(fn):
    """
    Applies `fn` to the `inputs`. `fn` should return one or multiple `storch.Tensor`s.
    `fn` should not call `storch.stochastic` or `storch.deterministic`. `inputs` can include `storch.Tensor`s.
    :param fn:
    :return:
    """
    def wrapper(*args, **kwargs):
        if storch.wrappers._context_stochastic or storch.wrappers._context_deterministic:
            raise RuntimeError("Cannot call storch.stochastic from within a stochastic or deterministic context.")
        storch.wrappers._context_stochastic = True
        # Save the parents
        args, parents, plates = _unwrap(*args, **kwargs)
        storch.wrappers._stochastic_parents = parents

        outputs = fn(*args, **kwargs)

        # Add parents to the outputs
        if isinstance(outputs, Iterable):
            processed_outputs = []
            for o in outputs:
                processed_outputs.append(_process_stochastic(o, parents, plates))
        else:
            processed_outputs = _process_stochastic(outputs, parents, plates)
        storch.wrappers._context_stochastic = False
        storch.wrappers._stochastic_parents = []
        return processed_outputs
    return wrapper