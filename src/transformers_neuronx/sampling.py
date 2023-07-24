# Copyright Amazon Web Services and its Affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import torch


@torch.no_grad()
def simple_sample(model, input_ids, start_ids, sequence_length, eos_token_id=2, top_k=50):
    # populate key/value caches according to the prompt text
    _, start = input_ids.shape
    cache_ids = torch.arange(start, dtype=torch.int32)
    next_token_scores = model(input_ids, cache_ids, start_ids)
    return sample_loop(model, input_ids, start_ids, next_token_scores, sequence_length,
                       eos_token_id, top_k)


def sample_tokens(model, input_ids, start_ids=None, sequence_length=128):
    """
    A sampling loop for a model that emits selected tokens.

    This sampling loop should be used when the token selection is built into
    the model itself.
    """
    _, start = input_ids.shape
    cache_ids = torch.arange(start, dtype=torch.int32)
    next_tokens = model(input_ids, cache_ids, start_ids)

    tokens = [input_ids]
    for cur_len in range(start, sequence_length):

        next_tokens = next_tokens[..., -1:]
        tokens.append(next_tokens)

        # forward pass to get next token
        cache_ids = torch.as_tensor([cur_len], dtype=torch.int32)
        next_tokens = model(next_tokens, cache_ids, start_ids)

    return torch.cat(tokens, dim=-1)


def sample_greedy(model, input_ids, start_ids=None, sequence_length=128):
    """
    A sampling loop that selects tokens according to the most probable score.

    This is useful as a reference implementation for on-device greedy sampling.
    """
    _, start = input_ids.shape
    cache_ids = torch.arange(start, dtype=torch.int32)
    next_token_scores = model(input_ids, cache_ids, start_ids)

    tokens = [input_ids]
    for cur_len in range(start, sequence_length):

        # greedy sample
        inputs = torch.argmax(next_token_scores, dim=1, keepdim=True)
        tokens.append(inputs)

        # forward pass to get next token
        cache_ids = torch.as_tensor([cur_len], dtype=torch.int32)
        next_token_scores = model(inputs, cache_ids, start_ids)

    return torch.cat(tokens, dim=-1)


def sample_loop(model, input_ids, start_ids, next_token_scores, sequence_length, eos_token_id=2,
                top_k=50):
    tokens = [input_ids]
    _, start = input_ids.shape
    for cur_len in range(start, sequence_length):
        next_len = cur_len + 1

        # don't sample EOS
        next_token_scores[:, eos_token_id] = -float('inf')

        # Remove all tokens with a probability less than the last token of the top-k
        topk_values, topk_indices = torch.topk(next_token_scores, top_k)

        # sample
        probs = torch.nn.functional.softmax(topk_values, dim=-1)
        inputs_in_topk = torch.multinomial(probs, num_samples=1, replacement=True)
        inputs = torch.gather(topk_indices, 1, inputs_in_topk)
        tokens.append(inputs)

        if next_len >= sequence_length:
            break

        # forward pass to get next token
        cache_ids = torch.as_tensor([cur_len], dtype=torch.int32)
        next_token_scores = model(inputs, cache_ids, start_ids)
    return torch.cat(tokens, dim=-1)


def validate_top_k_top_p_min_tokens_to_keep(top_k, top_p, min_tokens_to_keep):
    if top_k is not None and (not isinstance(top_k, int) or not (top_k > 0)):
        raise ValueError('top_k` has to be a strictly positive int.')

    if top_p is not None and (not isinstance(top_p, float) or not (0.0 < top_p <= 1.0)):
        raise ValueError('top_p` has to be a strictly positive float that less than or equal to 1.0.')

    if min_tokens_to_keep is not None and (not isinstance(min_tokens_to_keep, int) or min_tokens_to_keep < 0):
        raise ValueError('min_tokens_to_keep` has to be a non-negative int.')


def top_k_top_p_filtering(scores, top_k, top_p, min_tokens_to_keep=1):
    validate_top_k_top_p_min_tokens_to_keep(top_k, top_p, min_tokens_to_keep)

    input_size = scores.size(dim=-1)

    def safe_size(size):
        return min(max(size, min_tokens_to_keep), input_size)

    def input_value_and_indices():
        return scores, torch.arange(start=0, end=input_size).repeat([scores.size(dim=0), 1])

    def filter_by_top_k():
        return torch.topk(scores, safe_size(top_k))

    def filter_by_top_p(indices=None):
        """
        indices==None indicates that top_k filtering was not performed, perform top_p filtering on the entire scores.
        Otherwise, performs top_p filtering on the result of top_k filtering, and calculating cumulative probabilities
        only on the filtered result from top_k filtering.
        """
        scores_to_filter = scores if indices is None else torch.index_select(scores, dim=-1, index=indices[0])
        sorted_scores, sorted_indices = torch.sort(scores_to_filter, descending=True)
        cumulative_probs = torch.cumsum(torch.nn.functional.softmax(sorted_scores, dim=-1), dim=-1)
        n_to_keep = (cumulative_probs <= top_p).int().sum(dim=-1).apply_(safe_size)
        scores_to_keep, indices_to_keep = sorted_scores[:, 0:max(n_to_keep)], sorted_indices[:, 0:max(n_to_keep)]

        # the to_keep tensors kept max(n_to_keep). n_to_keep is an array of ints, each indicates the number of
        # scores to keep for the respective sequence in a batch. since indices_to_keep need to be returned as a
        # whole matrix, we need to set the values that correspond to unwanted indices, those that are exceeds n_to_keep
        # for their respective sequences, to -inf. This way subsequent sampling logic will not pick up these unwanted
        # token indices.
        for i, seq in enumerate(scores_to_keep):
            scores_to_keep[i, n_to_keep[i].item():] = -float('inf')

        if indices is not None:
            # Map to original indices
            indices_to_keep = torch.index_select(indices, dim=-1, index=indices_to_keep[0])

        return scores_to_keep, indices_to_keep

    if (top_k is None and top_p is None) or min_tokens_to_keep > input_size:
        # Nothing to filter
        return input_value_and_indices()

    # Only filter by top_k
    if top_k is not None and top_p is None:
        return filter_by_top_k()

    # Only filter by top_p
    if top_k is None and top_p is not None:
        return filter_by_top_p()

    # Filter by top_k followed by top_p
    return filter_by_top_p(filter_by_top_k()[1])


def sample_loop_llama(model, input_ids, start_ids, next_token_scores, sequence_length, eos_token_id=2,
                      top_k=50, top_p=1.0, temperature=1.0):
    validate_top_k_top_p_min_tokens_to_keep(top_k, top_p, None)

    if not isinstance(temperature, float) or not (temperature > 0):
        raise ValueError('temperature` has to be a strictly positive float.')

    # Flags, one per sequence in a batch, to indicate if a sequence hit eos_token_id
    done_flags = torch.full((input_ids.size(dim=0), 1), False)
    tokens = [input_ids]
    _, start = input_ids.shape

    for cur_len in range(start, sequence_length):
        next_len = cur_len + 1

        if temperature != 1.0:
            next_token_scores /= temperature

        top_values, top_indices = top_k_top_p_filtering(next_token_scores, top_k=top_k, top_p=top_p)

        # sample
        probs = torch.nn.functional.softmax(top_values, dim=-1)
        inputs_in_topk = torch.multinomial(probs, num_samples=1, replacement=True)
        inputs = torch.gather(top_indices, 1, inputs_in_topk)

        # Update done flags.
        done_flags = torch.logical_or(done_flags, inputs == eos_token_id)
        # Update token id to be eos_token_id if the corresponding done flag is True. For a batch,
        # this means that, while every sequence in the batch has the same length, a sequence that
        # encounters eos_token_id earlier will be filled with eos_token_ids post the first appearance
        # of eos_token_id.
        tokens.append(torch.where(done_flags == True, eos_token_id, inputs))

        if next_len >= sequence_length or torch.all(done_flags == True):
            break

        # forward pass to get next token
        cache_ids = torch.as_tensor([cur_len], dtype=torch.int32)
        next_token_scores = model(inputs, cache_ids, start_ids)
    return torch.cat(tokens, dim=-1)


@torch.no_grad()
def sample_llama(model, input_ids, start_ids, sequence_length, eos_token_id=2, top_k=50, top_p=1.0, temperature=1.0):
    validate_top_k_top_p_min_tokens_to_keep(top_k, top_p, None)

    # populate key/value caches according to the prompt text
    _, start = input_ids.shape
    cache_ids = torch.arange(start, dtype=torch.int32)
    next_token_scores = model(input_ids, cache_ids, start_ids)
    return sample_loop_llama(
        model, input_ids, start_ids, next_token_scores, sequence_length, eos_token_id, top_k, top_p, temperature
    )
