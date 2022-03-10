# Copyright 2017--2022 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not
# use this file except in compliance with the License. A copy of the License
# is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.
import logging
import os
import sys
from contextlib import ExitStack
from typing import Any, Dict, List
from unittest.mock import patch

import numpy as np

import sockeye.score
import sockeye.translate
from sockeye import constants as C
from sockeye.test_utils import run_train_translate, run_translate_restrict, \
    TRANSLATE_PARAMS_COMMON, TRANSLATE_WITH_FACTORS_COMMON, \
    collect_translate_output_and_scores, SCORE_PARAMS_COMMON, \
    SCORE_WITH_SOURCE_FACTORS_COMMON, SCORE_WITH_TARGET_FACTORS_COMMON, TRANSLATE_WITH_JSON_FORMAT

logger = logging.getLogger(__name__)


def check_train_translate(train_params: str,
                          translate_params: str,
                          data: Dict[str, Any],
                          use_prepared_data: bool,
                          max_seq_len: int,
                          compare_output: bool = True,
                          seed: int = 13) -> Dict[str, Any]:
    """
    Tests core features (training, inference).
    """
    # train model and translate test set
    data = run_train_translate(train_params=train_params,
                               translate_params=translate_params,
                               data=data,
                               use_prepared_data=use_prepared_data,
                               max_seq_len=max_seq_len,
                               seed=seed)

    # Test equivalence of batch decoding
    if 'greedy' not in translate_params:
        translate_params_batch = translate_params + " --batch-size 2"
        test_translate_equivalence(data, translate_params_batch, compare_output=True)

    # Run translate with restrict-lexicon
    data = run_translate_restrict(data, translate_params)

    test_translate_equivalence(data, translate_params, compare_output=True)

    # Test scoring by ensuring that the sockeye.scoring module produces the same scores when scoring the output
    # of sockeye.translate. However, since this training is on very small datasets, the output of sockeye.translate
    # is often pure garbage or empty and cannot be scored. So we only try to score if we have some valid output
    # to work with.
    # Only run scoring under these conditions. Why?
    # - translate splits up too-long sentences and translates them in sequence, invalidating the score, so skip that
    # - scoring requires valid translation output to compare against
    if '--max-input-length' not in translate_params and _translate_output_is_valid(data['test_outputs']) \
            and _translate_output_is_valid(data['test_with_target_prefix_outputs']) and 'greedy' not in translate_params:
        test_scoring(data, translate_params, compare_output)

    # Test correct prediction of target factors if enabled
    if compare_output and 'train_target_factors' in data:
        test_odd_even_target_factors(data)

    return data


def test_translate_equivalence(data: Dict[str, Any], translate_params_equiv: str, compare_output: bool):
    """
    Tests whether the output and scores generated by sockeye.translate with translate_params_equiv are equal to
    the previously generated outputs, referenced in the data dictionary.
    """
    out_path = os.path.join(data['work_dir'], "test.out.equiv")
    out_with_target_prefix_path = os.path.join(data['work_dir'], "test_with_target_prefix.out.equiv")

    # First set of params (with target prefix in JSON format)
    params = "{} {} {}".format(sockeye.translate.__file__,
                               TRANSLATE_PARAMS_COMMON.format(model=data['model'],
                                                              input=data['test_source_with_target_prefix'],
                                                              output=out_with_target_prefix_path),
                               translate_params_equiv)
    params += TRANSLATE_WITH_JSON_FORMAT
    with patch.object(sys, "argv", params.split()):
        sockeye.translate.main()

    # Collect translate outputs and scores
    translate_outputs_with_target_prefix_equiv = collect_translate_output_and_scores(out_with_target_prefix_path)

    # Second set of params (without using target prefix)
    params = "{} {} {}".format(sockeye.translate.__file__,
                               TRANSLATE_PARAMS_COMMON.format(model=data['model'],
                                                              input=data['test_source'],
                                                              output=out_path),
                               translate_params_equiv)
    if 'test_source_factors' in data:
        params += TRANSLATE_WITH_FACTORS_COMMON.format(input_factors=" ".join(data['test_source_factors']))
    with patch.object(sys, "argv", params.split()):
        sockeye.translate.main()
    # Collect translate outputs and scores
    translate_outputs_equiv = collect_translate_output_and_scores(out_path)

    assert 'test_outputs' in data
    assert 'test_with_target_prefix_outputs' in data
    assert len(data['test_outputs']) == len(data['test_with_target_prefix_outputs']) == len(translate_outputs_with_target_prefix_equiv) == len(translate_outputs_equiv)
    if compare_output:
        for json_output, json_output_with_target_prefix, json_output_equiv, json_output_with_target_prefix_equiv in zip(data['test_outputs'], data['test_with_target_prefix_outputs'], translate_outputs_equiv, translate_outputs_with_target_prefix_equiv):
            assert json_output['translation'] == json_output_equiv['translation'], \
                f"'{json_output['translation']}' vs. '{json_output_equiv['translation']}'"
            assert json_output_with_target_prefix['translation'] == json_output_with_target_prefix_equiv['translation'], \
                f"'{json_output_with_target_prefix['translation']}' vs. '{json_output_with_target_prefix_equiv['translation']}'"
            assert abs(json_output['score'] - json_output_equiv['score']) < 0.01 or \
                   np.isnan(json_output['score'] - json_output_equiv['score']), \
                f"'{json_output['score']}' vs. '{ json_output_equiv['score']}'"
            assert abs(json_output_with_target_prefix['score'] - json_output_with_target_prefix_equiv['score']) < 0.01 or \
                   np.isnan(json_output_with_target_prefix['score'] - json_output_with_target_prefix_equiv['score']), \
                f"'{json_output_with_target_prefix['score']}' vs. '{ json_output_with_target_prefix_equiv['score']}'"

            # Check translation output always includes target prefix tokens
            prefix = json_output_with_target_prefix['target_prefix'].split()
            translation = json_output_with_target_prefix['translation'].split()
            ending = min(len(prefix), len(translation))
            assert prefix[:ending] == translation[:ending], \
                f"'{prefix[:ending]}' vs. '{translation[:ending]}'"

            # Check translation output factors always include target prefix factors
            if 'target_prefix_factors' in json_output_with_target_prefix:
                prefix = json_output_with_target_prefix['target_prefix_factors']
                if len(prefix) > 0:
                    for j in range(1, len(prefix) + 1):
                        factors_from_translation = json_output_with_target_prefix[f'factor{j}']
                        ending = min(len(prefix[j - 1]), len(factors_from_translation))
                        assert prefix[j - 1][:ending] == factors_from_translation[:ending], \
                            f"'{prefix[j - 1][:ending]}' vs. '{factors_from_translation[:ending]}' from . '{json_output_with_target_prefix}'"


def test_scoring(data: Dict[str, Any], translate_params: str, test_similar_scores: bool):
    """
    Tests the scoring CLI and checks for score equivalence with previously generated translate scores.
    """
    # Translate params that affect the score need to be used for scoring as well.
    relevant_params = {'--brevity-penalty-type',
                       '--brevity-penalty-weight',
                       '--brevity-penalty-constant-length-ratio',
                       '--length-penalty-alpha',
                       '--length-penalty-beta'}
    score_params = ''
    params = translate_params.split()
    for i, param in enumerate(params):
        if param in relevant_params:
            score_params = '{} {}'.format(param, params[i + 1])
    out_path = os.path.join(data['work_dir'], "score.out")
    out_with_target_prefix_path = os.path.join(data['work_dir'], "score_with_target_prefix.out")

    # write translate outputs as target file for scoring and collect tokens
    # also optionally collect factor outputs
    target_path = os.path.join(data['work_dir'], "score.target")
    target_factor_paths = [os.path.join(data['work_dir'], "score.target.factor%d" % i) for i, _ in
                           enumerate(data.get('test_target_factors', []), 1)]
    with open(target_path, 'w') as target_out, ExitStack() as exit_stack:
        target_factor_outs = [exit_stack.enter_context(open(p, 'w')) for p in target_factor_paths]
        for json_output in data['test_outputs']:
            print(json_output['translation'], file=target_out)
            for i, factor_out in enumerate(target_factor_outs, 1):
                factor = json_output[f'factor{i}']
                print(factor, file=factor_out)

    target_with_target_prefix_path = os.path.join(data['work_dir'], "score_with_target_prefix.target")
    target_with_target_prefix_factor_paths = [os.path.join(data['work_dir'], f"score_with_target_prefix.target.factor{i}") for i, _ in
                           enumerate(data.get('test_target_factors', []), 1)]
    with open(target_with_target_prefix_path, 'w') as target_out, ExitStack() as exit_stack:
        target_factor_outs = [exit_stack.enter_context(open(p, 'w')) for p in target_with_target_prefix_factor_paths]
        for json_output in data['test_with_target_prefix_outputs']:
            print(json_output['translation'], file=target_out)
            for i, factor_out in enumerate(target_factor_outs, 1):
                factor = json_output[f'factor{i}']
                print(factor, file=factor_out)


    # First set of params (with target prefix in JSON format)
    params = "{} {} {}".format(sockeye.score.__file__,
                               SCORE_PARAMS_COMMON.format(model=data['model'],
                                                          source=data['test_source'],
                                                          target=target_with_target_prefix_path,
                                                          output=out_with_target_prefix_path),
                               score_params)
    if 'test_source_factors' in data:
        params += SCORE_WITH_SOURCE_FACTORS_COMMON.format(source_factors=" ".join(data['test_source_factors']))
    if target_with_target_prefix_factor_paths:
        params += SCORE_WITH_TARGET_FACTORS_COMMON.format(target_factors=" ".join(target_with_target_prefix_factor_paths))

    logger.info("Scoring with params %s", params)
    with patch.object(sys, "argv", params.split()):
        sockeye.score.main()

    # Collect scores from output file
    with open(out_with_target_prefix_path) as score_out:
        data_scoring_with_target_prefix = [[float(x) for x in line.strip().split('\t')] for line in score_out]

    # Second set of params (without target prefix)
    params = "{} {} {}".format(sockeye.score.__file__,
                               SCORE_PARAMS_COMMON.format(model=data['model'],
                                                          source=data['test_source'],
                                                          target=target_path,
                                                          output=out_path),
                               score_params)
    if 'test_source_factors' in data:
        params += SCORE_WITH_SOURCE_FACTORS_COMMON.format(source_factors=" ".join(data['test_source_factors']))
    if target_factor_paths:
        params += SCORE_WITH_TARGET_FACTORS_COMMON.format(target_factors=" ".join(target_factor_paths))

    logger.info("Scoring with params %s", params)
    with patch.object(sys, "argv", params.split()):
        sockeye.score.main()

    # Collect scores from output file
    with open(out_path) as score_out:
        data_scoring = [[float(x) for x in line.strip().split('\t')] for line in score_out]

    if test_similar_scores:
        for inp, translate_json, translate_with_target_prefix_json, score_scores, score_with_target_prefix_scores in zip\
          (data['test_inputs'], data['test_outputs'], data['test_with_target_prefix_outputs'], data_scoring, data_scoring_with_target_prefix):
            score_score, *factor_scores = score_scores
            translate_tokens = translate_json['translation'].split()
            translate_score = translate_json['score']
            logger.info("tokens: %s || translate score: %.4f || score score: %.4f",
                        translate_tokens, translate_score, score_score)
            assert (translate_score == -np.inf and score_score == -np.inf) or np.isclose(translate_score,
                                                                                         score_score,
                                                                                         atol=1e-06),\
                "input: %s || tokens: %s || translate score: %.6f || score score: %.6f" % (inp, translate_tokens,
                                                                                           translate_score,
                                                                                           score_score)
            score_score, *factor_scores = score_with_target_prefix_scores
            translate_tokens = translate_with_target_prefix_json['translation'].split()
            translate_score = translate_with_target_prefix_json['score']
            logger.info("tokens: %s || translate score: %.4f || score score: %.4f",
                        translate_tokens, translate_score, score_score)
            assert (translate_score == -np.inf and score_score == -np.inf) or np.isclose(translate_score,
                                                                                         score_score,
                                                                                         atol=1e-06),\
                "input: %s || tokens: %s || translate score: %.6f || score score: %.6f" % (inp, translate_tokens,
                                                                                           translate_score,
                                                                                           score_score)


def _translate_output_is_valid(translate_outputs: List[str]) -> bool:
    """
    True if there are invalid tokens in out_path, or if no valid outputs were found.
    """
    # At least one output must be non-empty
    found_valid_output = False
    bad_tokens = set(C.VOCAB_SYMBOLS)
    for json_output in translate_outputs:
        if json_output and 'translation' in json_output:
            found_valid_output = True
        if any(token for token in json_output['translation'].split() if token in bad_tokens):
            # There must be no bad tokens
            return False
    return found_valid_output


def test_odd_even_target_factors(data: Dict):
    num_target_factors = len(data['train_target_factors'])
    for json in data['test_outputs']:
        factor_keys = [k for k in json.keys() if k.startswith("factor") and not k.endswith("score")]
        assert len(factor_keys) == num_target_factors
        primary_tokens = json['translation'].split()
        secondary_factor_tokens = [json[factor_key].split() for factor_key in factor_keys]
        for factor_tokens in secondary_factor_tokens:
            assert len(factor_tokens) == len(primary_tokens)
            print(primary_tokens, factor_tokens)
            for primary_token, factor_token in zip(primary_tokens, factor_tokens):
                try:
                    if int(primary_token) % 2 == 0:
                        assert factor_token == 'e'
                    else:
                        assert factor_token == 'o'
                except ValueError:
                    logger.warning("primary token cannot be converted to int, skipping")
                    continue
