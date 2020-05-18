from __future__ import annotations

import itertools
from abc import abstractmethod
from typing import Union, List, Optional, Tuple

from storch.sampling.method import SamplingMethod
from torch.distributions import Distribution
import storch
import torch


class AncestralPlate(storch.Plate):
    def __init__(
        self,
        name: str,
        n: int,
        parents: List[storch.Plate],
        variable_index: int,
        parent_plate: AncestralPlate,
        selected_samples: storch.Tensor,
        log_probs: storch.Tensor,
        weight: Optional[storch.Tensor] = None,
    ):
        super().__init__(name, n, parents, weight)
        assert (not parent_plate and variable_index == 0) or (
            parent_plate.n <= self.n and parent_plate.variable_index < variable_index
        )
        self.parent_plate = parent_plate
        self.selected_samples = selected_samples
        self.log_probs = storch.Tensor(
            log_probs._tensor, [log_probs], log_probs.plates + [self]
        )
        self.variable_index = variable_index
        self._in_recursion = False
        self._override_equality = False

    def __eq__(self, other):
        if self._override_equality:
            return other.name == self.name
        return (
            super().__eq__(other)
            and isinstance(other, AncestralPlate)
            and self.variable_index == other.variable_index
        )

    def __repr__(self):
        return (
            "(Ancestral, " + self.variable_index.__repr__() + super().__repr__() + ")"
        )

    def on_collecting_args(self, plates: [storch.Plate]) -> bool:
        """
        Filter the collected plates to only keep the AncestralPlates (with the same name) that has the highest variable index.
        :param plates:
        :return:
        """
        if self._in_recursion:
            self._override_equality = True
        for plate in plates:
            if plate.name == self.name:
                if not isinstance(plate, AncestralPlate):
                    raise ValueError(
                        "Received a plate with name "
                        + plate.name
                        + " that is not also an AncestralPlate."
                    )
                if plate.variable_index > self.variable_index:
                    # Only keep ancestral plates with the highest variable index
                    return False
        return True

    def on_unwrap_tensor(self, tensor: storch.Tensor) -> storch.Tensor:
        """
        Gets called whenever the given tensor is being unwrapped and unsqueezed for batch use.
        This method should not be called on tensors whose variable index is higher than this plates.
        :param tensor: The input tensor that is being unwrapped
        :return: The tensor that will be unwrapped and unsqueezed in the future. Can be a modification of the input tensor.
        """
        if self._in_recursion:
            # Required when calling storch.gather in this method. It will call on_unwrap_tensor again.
            return tensor
        for i, plate in enumerate(tensor.multi_dim_plates()):
            if plate.name != self.name:
                continue
            assert isinstance(plate, AncestralPlate)
            if plate.variable_index == self.variable_index:
                return tensor
            # This is true by the filtering at on_collecting_args
            assert plate.variable_index < self.variable_index

            parent_plates = []
            current_plate = self

            # Collect the list of plates from the tensors variable index to this plates variable index
            while current_plate.variable_index != plate.variable_index:
                parent_plates.append(current_plate)
                current_plate = current_plate.parent_plate
            assert current_plate == plate

            # Go over all parent plates and gather their respective choices.
            for parent_plate in reversed(parent_plates):
                self._in_recursion = True
                expanded_selected_samples = expand_with_ignore_as(
                    parent_plate.selected_samples, tensor, self.name
                )
                self._override_equality = False
                # Gather what samples of the tensor are chosen by this plate (parent_plate)
                tensor = storch.gather(
                    tensor, parent_plate.name, expanded_selected_samples
                )
                self._in_recursion = False
                self._override_equality = False
            break
        return tensor


class SequenceDecoding(SamplingMethod):
    """
    Methods for generating sequences of discrete random variables.
    Examples: Simple ancestral sampling with replacement, beam search, Stochastic beam search (sampling without replacement)
    """

    EPS = 1e-8

    def __init__(self, plate_name: str, k: int):
        super().__init__(plate_name)
        self.k = k
        self.reset()

    def reset(self):
        super().reset()
        # Cumulative log probabilities of the samples
        self.joint_log_probs = None
        # Chosen parent samples at the previous sample step
        self.parent_indexing = None
        # The index of the currently sampled variable
        self.variable_index = 0
        # The previously created plates
        self.last_plate = None

    def sample(
        self,
        distr: Distribution,
        parents: [storch.Tensor],
        plates: [storch.Plate],
        requires_grad: bool,
    ) -> (torch.Tensor, storch.Plate):

        """
        Sample from the distribution given the sequence so far.
        :param distribution: The distribution to sample from
        :return:
        """

        # This code has three parts.
        # The first prepares all necessary tensors to make sure they can be easily indexed.
        # This part is quite long as there are many cases.
        # 1) There have not been any variables sampled so far.
        # 2) There have been variables sampled, but their results are NOT used to compute the input distribution.
        #    in other words, the variable to sample is independent of the other sampled variables. However,
        #    we should still keep track of the other sampled variables to make sure that it still samples without
        #    replacement properly. In this case, the ancestral plate is not in the plates attribute.
        #    We also have to make sure that we know in the future what samples are chosen for the _other_ samples.
        # 3) There have been parents sampled, and this variable is dependent on at least some of them.
        #    The plates list then contains the ancestral plate. We need to make sure we compute the joint log probs
        #    for the conditional samples (ie, based on the different sampled variables in the ancestral dimension).
        # The second part is a loop over all options for the event dimensions. This samples these conditionally
        # independent samples in sequence. It samples indexes, not events.
        # The third part after the loop uses the sampled indexes and matches it to the events to be used.

        # LEGEND FOR SHAPE COMMENTS
        # =========================
        # To make this code generalize to every bayesian network, complicated shape management is necessary.
        # The following are references to the shapes that are used within the method
        #
        # distr_plates: refers to the plates on the parameters of the distribution. Does *not* include
        #  the k? ancestral plate (possibly empty)
        # orig_distr_plates: refers to the plates on the parameters of the distribution, and *can* include
        #  the k? ancestral plate (possibly empty)
        # prev_plates: refers to the plates of the previous sampled variable in this swr sample (possibly empty)
        # plates: refers to all plates except this ancestral plate, of which there are amt_plates.
        #  It is composed of distr_plate x (ancstr_plates - distr_plates)
        # events: refers to the conditionally independent dimensions of the distribution (the distributions batch shape minus the plates)
        # k: refers to self.k
        # k?: refers to an optional plate dimension of this ancestral plate. It either doesn't exist, or is the sample
        #  dimension. If it exists, this means this sample is conditionally dependent on ancestors.
        # |D_yv|: refers to the *size* of the domain
        # amt_samples: refers to the current amount of sampled sequences. amt_samples <= k, but it can be lower if there
        #  are not enough events to sample from (eg |D_yv| < k)
        # event_shape: refers to the *shape* of the domain elements
        #  (can be 0, eg Categorical, or equal to |D_yv| for OneHotCategorical)

        orig_distr_plates = []
        for plate in plates:
            if plate.n > 1:
                orig_distr_plates.append(plate)
        # Sample using stochastic beam search
        # plates? x k x events x
        ancestral_distrplate_index = -1
        is_conditional_sample = False
        distr_plates = orig_distr_plates
        for i, plate in enumerate(orig_distr_plates):
            if plate.name == self.plate_name:
                ancestral_distrplate_index = i
                is_conditional_sample = True
                distr_plates = orig_distr_plates.copy()
                distr_plates.remove(plate)
                break

        # TODO: This doesn't properly combine two ancestral plates with the same name but different variable index
        #  (they should merge).
        all_plates = distr_plates.copy()
        prev_plate_shape = ()
        if self.variable_index > 0:
            # Previous variables have been sampled. add the prev_plates to all_plates
            for plate in self.joint_log_probs.plates:
                if plate not in distr_plates:
                    all_plates.append(plate)
                    prev_plate_shape = prev_plate_shape + (plate.n,)

        amt_plates = len(all_plates)
        amt_distr_plates = len(distr_plates)
        amt_orig_distr_plates = len(orig_distr_plates)
        if not distr.has_enumerate_support:
            raise ValueError("Can only decode distributions with enumerable support.")

        # |D_yv| x distr_plate[0] x ... x k? x ... x distr_plate[n-1] x events x event_shape
        support = distr.enumerate_support(expand=True)

        if ancestral_distrplate_index != -1:
            # Reduce ancestral dimension in the support. As the dimension is just an expanded version, this should
            # not change the underlying data.
            # |D_yv| x distr_plates x events x event_shape
            support = support[
                (..., 0)
                + (slice(None),) * (len(support.shape) - ancestral_distrplate_index - 2)
            ]

        # Equal to event_shape
        element_shape = distr.event_shape
        support_permutation = (
            tuple(range(1, amt_distr_plates + 1))
            + (0,)
            + tuple(range(amt_distr_plates + 1, len(support.shape)))
        )
        # distr_plates x |D_yv| x events x event_shape
        support = support.permute(support_permutation)

        if amt_plates != amt_distr_plates:
            # If previous samples had a plate that are not in the distribution plates, add these to the support.
            support = support[
                (slice(None),) * amt_distr_plates
                + (None,) * (amt_plates - amt_distr_plates)
            ]
            # plates x |D_yv| x events x event_shape
            support = support.expand(
                (-1,) * amt_distr_plates
                + prev_plate_shape
                + (-1,) * (len(support.shape) - amt_distr_plates - 1)
            )
        support = storch.Tensor(support, [], all_plates)

        # Equal to events: Shape for the different conditional independent dimensions
        event_shape = support.shape[
            amt_plates + 1 : -len(element_shape) if len(element_shape) > 0 else None
        ]

        with storch.ignore_wrapping():
            # |D_yv| x (|distr_plates| + |k?| + |event_dims|) * (1,) x |D_yv|
            support_non_expanded: torch.Tensor = distr.enumerate_support(expand=False)
            # Compute the log-probability of the different events
            # |D_yv| x distr_plate[0] x ... k? ... x distr_plate[n-1] x events
            d_log_probs = distr.log_prob(support_non_expanded)

            # Note: Use amt_orig_distr_plates here because it might include k? dimension. amt_distr_plates filters this one.
            # distr_plate[0] x ... k? ... x distr_plate[n-1] x |D_yv| x events
            d_log_probs = storch.Tensor(
                d_log_probs.permute(
                    tuple(range(1, amt_orig_distr_plates + 1))
                    + (0,)
                    + tuple(
                        range(
                            amt_orig_distr_plates + 1,
                            amt_orig_distr_plates + 1 + len(event_shape),
                        )
                    )
                ),
                [],
                orig_distr_plates,
            )
        if is_conditional_sample:
            # Gather the correct log probabilities
            # distr_plate[0] x ... k ... x distr_plate[n-1] x |D_yv| x events
            # TODO: Move this down below to the other scary TODO
            d_log_probs = self.last_plate.on_unwrap_tensor(d_log_probs)
            # Permute the dimensions of d_log_probs st the k dimension is after the plates.
            for i, plate in enumerate(d_log_probs.multi_dim_plates()):
                if plate.name == self.plate_name:
                    # k is present in the plates
                    d_log_probs.plates.remove(plate)
                    # distr_plates x k x |D_yv| x events
                    d_log_probs._tensor = d_log_probs._tensor.permute(
                        tuple(range(0, i))
                        + tuple(range(i + 1, amt_orig_distr_plates))
                        + (i,)
                        + tuple(range(amt_orig_distr_plates, len(d_log_probs.shape)))
                    )
                    break

        # Seperate classes into AbstractBeam class and this one, where this one doesn't compute all prerequisites.
        # AbstractBeam is for BeamSearch and SWOR (stochastic beam)

        # Do the decoding step given the prepared tensors
        samples, self.joint_log_probs, self.parent_indexing = self.decode(
            d_log_probs,
            support,
            self.joint_log_probs,
            is_conditional_sample,
            amt_plates,
            event_shape,
            element_shape,
        )

        k_index = 0
        if isinstance(samples, storch.Tensor):
            k_index = samples.plate_dims
            plates = samples.plates
            samples = samples._tensor

        plate_size = samples.shape[k_index]

        # Remove the ancestral plate, if it already happens to be in
        to_remove = None
        for plate in plates:
            if plate.name == self.plate_name:
                to_remove = plate
                break
        if to_remove:
            plates.remove(to_remove)

        # Create the newly updated plate
        self.last_plate = self.create_plate(plate_size, plates.copy())
        plates.append(self.last_plate)

        if self.parent_indexing is not None:
            self.parent_indexing.plates.append(self.last_plate)

        # Construct the stochastic tensor
        s_tensor = storch.StochasticTensor(
            samples,
            parents,
            plates,
            self.plate_name,
            plate_size,
            distr,
            requires_grad or self.joint_log_probs.requires_grad,
        )
        # Increase variable index
        self.variable_index += 1
        return s_tensor, self.last_plate

    @abstractmethod
    def decode(
        self,
        d_log_probs: storch.Tensor,
        support: storch.Tensor,
        joint_log_probs: Optional[storch.Tensor],
        is_conditional_sample: bool,
        amt_plates: int,
        event_shape: torch.Size,
        element_shape: torch.Size,
    ) -> (storch.Tensor, storch.Tensor, storch.Tensor):
        """
        Decode given the input arguments
        :param d_log_probs: Log probability given by the distribution. distr_plates x k? x |D_yv| x events
        :param support: The support of this distribution. plates x |D_yv| x events x event_shape
        :param joint_log_probs: The log probabilities of the samples so far. prev_plates x amt_samples
        :param is_conditional_sample: True if a parent has already been sampled. This means the plates are more complex!
        :param amt_plates: The total amount of plates in both the distribution and the previously sampled variables
        :param event_shape: The shape of the conditionally independent events
        :param element_shape: The shape of a domain element. (|D_yv|,) for `torch.distributions.OneHotCategorical`, otherwise (,).
        :return: 3-tuple of `storch.Tensor`. 1: The sampled value. 2: The new joint log probabilities of the samples.
        3: How the samples index the parent samples. Can just be a range if there is no choosing happening.
        """
        pass

    def create_plate(self, plate_size: int, plates: [storch.Plate]) -> AncestralPlate:
        return AncestralPlate(
            self.plate_name,
            plate_size,
            plates.copy(),
            self.variable_index,
            self.last_plate,
            self.parent_indexing,
            self.joint_log_probs,
            None,
        )


class IterDecoding(SequenceDecoding):
    def decode(
        self,
        d_log_probs: storch.Tensor,
        support: storch.Tensor,
        joint_log_probs: Optional[storch.Tensor],
        is_conditional_sample: bool,
        amt_plates: int,
        event_shape: torch.Size,
        element_shape: torch.Size,
    ) -> (storch.Tensor, storch.Tensor, storch.Tensor):
        """
        Decode given the input arguments
        :param d_log_probs: Log probability given by the distribution. distr_plates x k? x |D_yv| x events
        :param support: The support of this distribution. plates x |D_yv| x events x event_shape
        :param joint_log_probs: The log probabilities of the samples so far. None if nothing is sampled yet. prev_plates x amt_samples
        :param is_conditional_sample: True if a parent has already been sampled. This means the plates are more complex!
        :param amt_plates: The total amount of plates in both the distribution and the previously sampled variables
        :param event_shape: The shape of the conditionally independent events
        :param element_shape: The shape of a domain element. (|D_yv|,) for `torch.distributions.OneHotCategorical`, otherwise (,).
        :return: 3-tuple of `storch.Tensor`. 1: The sampled value. 2: The new joint log probabilities of the samples.
        3: How the samples index the parent samples. Can just be a range if there is no choosing happening.
        """
        ranges = []
        for size in event_shape:
            ranges.append(list(range(size)))

        amt_samples = 0
        parent_indexing = None
        if joint_log_probs is not None:
            # Initialize a tensor (self.parent_indexing) that keeps track of what samples link to previous choices of samples
            # Note that joint_log_probs.shape[-1] is amt_samples, not k. It's possible that amt_samples < k!
            amt_samples = joint_log_probs.shape[-1]
            # plates x k
            parent_indexing = support.new_zeros(
                size=support.shape[:amt_plates] + (self.k,), dtype=torch.long
            )

            # probably can go wrong if plates are missing.
            parent_indexing[..., :amt_samples] = left_expand_as(
                torch.arange(amt_samples), parent_indexing
            )
        # plates x k x events
        sampled_support_indices = support.new_zeros(
            size=support.shape[:amt_plates]  # plates
            + (self.k,)
            + support.shape[
                amt_plates + 1 : -len(element_shape) if len(element_shape) > 0 else None
            ],  # events
            dtype=torch.long,
        )
        # Sample independent tensors in sequence
        # Iterate over the different (conditionally) independent samples being taken (the events)
        for indices in itertools.product(*ranges):
            # Log probabilities of the different options for this sample step (event)
            # distr_plates x k? x |D_yv|
            yv_log_probs = d_log_probs[(...,) + indices]
            (
                sampled_support_indices,
                joint_log_probs,
                parent_indexing,
                amt_samples,
            ) = self.decode_step(
                indices,
                yv_log_probs,
                joint_log_probs,
                sampled_support_indices,
                parent_indexing,
                is_conditional_sample,
                amt_plates,
                amt_samples,
            )
        # Finally, index the support using the sampled indices to get the sample!
        if amt_samples < self.k:
            # plates x amt_samples x events
            sampled_support_indices = sampled_support_indices[
                (...,) + (slice(amt_samples),) + (slice(None),) * len(ranges)
            ]
        expanded_indices = right_expand_as(sampled_support_indices, support)
        sample = support.gather(dim=amt_plates, index=expanded_indices)
        return sample, joint_log_probs, parent_indexing

    @abstractmethod
    def decode_step(
        self,
        indices: Tuple[int],
        yv_log_probs: storch.Tensor,
        joint_log_probs: Optional[storch.Tensor],
        sampled_support_indices: Optional[storch.Tensor],
        parent_indexing: Optional[storch.Tensor],
        is_conditional_sample: bool,
        amt_plates: int,
        amt_samples: int,
    ) -> (storch.Tensor, storch.Tensor, storch.Tensor, int):
        """
        Decode given the input arguments for a specific event
        :param indices: Tuple of integers indexing the current event to sample.
        :param yv_log_probs:  Log probabilities of the different options for this event. distr_plates x k? x |D_yv|
        :param joint_log_probs: The log probabilities of the samples so far. None if `not is_conditional_sample`. prev_plates x amt_samples
        :param sampled_support_indices: Tensor of samples so far. None if this is the first set of indices. plates x k x events
        :param parent_indexing: Tensor indexing the parent sample. None if `not is_conditional_sample`.
        :param is_conditional_sample: True if a parent has already been sampled. This means the plates are more complex!
        :param amt_plates: The total amount of plates in both the distribution and the previously sampled variables
        :param amt_samples: The amount of active samples.
        :return: 3-tuple of `storch.Tensor`. 1: sampled_support_indices, with `:, indices` referring to the indices for the support.
        2: The updated `joint_log_probs` of the samples.
        3: The updated `parent_indexing`. How the samples index the parent samples. Can just return parent_indexing if nothing happens.
        4: The amount of active samples after this step.
        """
        pass


def expand_with_ignore_as(
    tensor, expand_as, ignore_dim: Union[str, int]
) -> torch.Tensor:
    """
    Expands the tensor like expand_as, but ignores a single dimension.
    Ie, if tensor is of size a x b,  expand_as of size d x a x c and dim=-1, then the return will be of size d x a x b.
    It also automatically expands all plate dimensions correctly.
    :param ignore_dim: Can be a string referring to the plate dimension
    """
    # diff = expand_as.ndim - tensor.ndim
    def _expand_with_ignore(tensor, expand_as, dim: int):
        new_dims = expand_as.ndim - tensor.ndim
        # after_dims = tensor.ndim - dim
        return tensor[(...,) + (None,) * new_dims].expand(
            expand_as.shape[:dim]
            + (-1,)
            + (expand_as.shape[dim + 1 :] if dim != -1 else ())
        )

    if isinstance(ignore_dim, str):
        return storch.deterministic(
            _expand_with_ignore, expand_plates=True, dim=ignore_dim
        )(tensor, expand_as)
    return storch.deterministic(_expand_with_ignore, expand_plates=True)(
        tensor, expand_as, ignore_dim
    )


@storch.deterministic(l_broadcast=False)
def right_expand_as(tensor, expand_as):
    diff = expand_as.ndim - tensor.ndim
    return tensor[(...,) + (None,) * diff].expand(
        (-1,) * tensor.ndim + expand_as.shape[tensor.ndim :]
    )


def left_expand_as(tensor, expand_as):
    diff = expand_as.ndim - tensor.ndim
    return tensor[(None,) * diff].expand(expand_as.shape[:diff] + (-1,) * tensor.ndim)
