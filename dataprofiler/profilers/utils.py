"""Contains functions for profilers."""
import collections
import copy
import datetime
import functools
import math
import multiprocessing as mp
import os
import time
import warnings
from itertools import islice

import numpy as np
import psutil
import scipy

from dataprofiler import settings


def dict_merge(dct, merge_dct):
    # Recursive dictionary merge
    # Copyright (C) 2016 Paul Durivage <pauldurivage+github@gmail.com>
    #
    # This program is free software: you can redistribute it and/or modify
    # it under the terms of the GNU General Public License as published by
    # the Free Software Foundation, either version 3 of the License, or
    # (at your option) any later version.
    #
    # This program is distributed in the hope that it will be useful,
    # but WITHOUT ANY WARRANTY; without even the implied warranty of
    # MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    # GNU General Public License for more details.
    #
    # You should have received a copy of the GNU General Public License
    # along with this program.  If not, see <https://www.gnu.org/licenses/>.
    """
    Merge dict recursively.

    Inspired by :meth:``dict.update()``, instead of updating only
    top-level keys, dict_merge recurses down into dicts nested
    to an arbitrary depth, updating keys. The ``merge_dct`` is
    merged into ``dct``.

    :param dct: dict onto which the merge is executed
    :param merge_dct: dct merged into dct
    :return: None
    """
    for k, v in merge_dct.items():
        if (
            k in dct
            and isinstance(dct[k], dict)
            and isinstance(merge_dct[k], collections.abc.Mapping)
        ):
            dict_merge(dct[k], merge_dct[k])
        else:
            dct[k] = merge_dct[k]


class KeyDict(collections.defaultdict):
    """
    Helper class for sample_in_chunks.

    Allows keys that are missing to become the values for that key.
    From:
    https://www.drmaciver.com/2018/01/lazy-fisher-yates-shuffling-for-precise-rejection-sampling/
    """

    def __missing__(self, key):
        """Return key if key nonexistent."""
        return key


def _combine_unique_sets(a, b):
    """
    Unify two lists.

    :type a: list
    :type b: list
    :rtype: list
    """
    combined_list = None
    if not a and not b:
        combined_list = list()
    elif not a:
        combined_list = set(b)
    elif not b:
        combined_list = set(a)
    else:
        combined_list = set().union(a, b)
    return list(combined_list)


def shuffle_in_chunks(data_length, chunk_size):
    """
    Create shuffled indexes in chunks.

    This reduces the cost of having to create all indexes,
    but only of that what is needed.
    Initial Code idea from:
    https://www.drmaciver.com/2018/01/lazy-fisher-yates-shuffling-for-precise-rejection-sampling/

    :param data_length: length of data to be shuffled
    :param chunk_size: size of shuffled chunks
    :return: list of shuffled indices of chunk size
    """
    if not data_length or data_length == 0 or not chunk_size or chunk_size == 0:
        return []

    rng = np.random.default_rng(settings._seed)

    if "DATAPROFILER_SEED" in os.environ and settings._seed is None:
        try:
            seed_value = int(os.environ.get("DATAPROFILER_SEED"))
            rng = np.random.default_rng(seed_value)
        except ValueError:
            warnings.warn("Seed should be an integer", RuntimeWarning)

    indices = KeyDict()
    j = 0

    # loop through all chunks
    for chunk_ind in range(max(math.ceil(data_length / chunk_size), 1)):

        # determine the chunk size and preallocate an array
        true_chunk_size = min(chunk_size, data_length - chunk_size * chunk_ind)
        values = [-1] * true_chunk_size

        # Generate random list of indexes
        lower_bound_list = np.array(range(j, j + true_chunk_size))
        random_list = rng.integers(lower_bound_list, data_length)

        # shuffle the indexes
        for count in range(true_chunk_size):

            # get a random index to swap and swap it with j
            k = random_list[count]
            indices[j], indices[k] = indices[k], indices[j]

            # set the swapped value to the output
            values[count] = indices[j]

            # increment so as not to include values already swapped
            j += 1

        yield values


def warn_on_profile(col_profile, e):
    """
    Return a warning if a given profile errors (tensorflow typically).

    :param col_profile: Name of the column profile
    :type col_profile: str
    :param e: Error message from profiler error
    :type e: Exception
    """
    import warnings

    warning_msg = "\n\n!!! WARNING Partial Profiler Failure !!!\n\n"
    warning_msg += "Profiling Type: {}".format(col_profile)
    warning_msg += "\nException: {}".format(type(e).__name__)
    warning_msg += "\nMessage: {}".format(e)
    # This is considered a major error
    if type(e).__name__ == "ValueError":
        raise ValueError(e)
    warning_msg += "\n\nFor labeler errors, try installing "
    warning_msg += "the extra ml requirements via:\n\n"
    warning_msg += "$ pip install dataprofiler[ml] --user\n\n"
    warnings.warn(warning_msg, RuntimeWarning, stacklevel=2)


def partition(data, chunk_size):
    """
    Create a generator which returns data in specified chunk size.

    :param data: list, dataframe, etc
    :type data: list, dataframe, etc
    :param chunk_size: size of partition to return
    :type chunk_size: int
    """
    for idx in range(0, len(data), chunk_size):
        yield data[idx : idx + chunk_size]


def suggest_pool_size(data_size=None, cols=None):
    """
    Suggest the pool size based on resources.

    :param data_size: size of the dataset
    :type data_size: int
    :param cols: columns of the dataset
    :type cols: int
    :return suggested_pool_size: suggeseted pool size
    :rtype suggested_pool_size: int
    """
    # Return if there's no data_size
    if data_size is None:
        return None

    try:
        # Determine safest level of processes based on memory
        mb = 1000000
        svmem = psutil.virtual_memory()
        max_pool_mem = (data_size * 50) / (svmem.available / mb)
    except NotImplementedError:
        max_pool_mem = 4

    try:
        # Determine safest level of processes based on CPUs
        max_pool_cpu = psutil.cpu_count() - 1
    except NotImplementedError:
        max_pool_cpu = 1

    # Limit to cols if less than threads
    suggested_pool_size = min(max_pool_mem, max_pool_cpu)
    if cols is not None:
        suggested_pool_size = min(suggested_pool_size, cols)

    return suggested_pool_size


def generate_pool(max_pool_size=None, data_size=None, cols=None):
    """
    Generate a multiprocessing pool to allocate functions too.

    :param max_pool_size: Max number of processes assigned to the pool
    :type max_pool_size: Union[int, None]
    :param data_size: size of the dataset
    :type data_size: int
    :param cols: columns of the dataset
    :type cols: int
    :return pool: Multiprocessing pool to allocate processes to
    :rtype pool: Multiproessing.Pool
    :return cpu_count: Number of processes (cpu bound) to utilize
    :rtype cpu_count: int
    """
    suggested_pool_size = suggest_pool_size(data_size, cols)
    if max_pool_size is None or suggested_pool_size is None:
        max_pool_size = suggested_pool_size

    # Always leave 1 cores free
    pool = None
    if max_pool_size is not None and max_pool_size > 2:
        try:
            pool = mp.Pool(max_pool_size)
        except Exception:
            pool = None
            warnings.warn(
                "Multiprocessing disabled, please change the multiprocessing"
                + " start method, via: multiprocessing.set_start_method(<method>)"
                + " Possible methods include: fork, spawn, forkserver, None"
            )

    return pool, max_pool_size


def overlap(x1, x2, y1, y2):
    """Return True iff [x1:x2] overlaps with [y1:y2]."""
    if not all([isinstance(i, int) for i in [x1, x2, y1, y2]]):
        return False
    return (y1 <= x1 <= y2) or (y1 <= x2 <= y2) or (x1 <= y1 <= x2) or (x1 <= y2 <= x2)


def add_nested_dictionaries(first_dict, second_dict):
    """
    Merge two dictionaries together and add values together.

    :param first_dict: dictionary to be merged
    :type first_dict: dict
    :param second_dict: dictionary to be merged
    :type second_dict: dict
    :return: merged dictionary
    """
    merged_dict = {}
    if isinstance(first_dict, collections.defaultdict):
        merged_dict = collections.defaultdict(first_dict.default_factory)

    for item in first_dict:
        if item in second_dict:
            if isinstance(first_dict[item], dict):
                merged_dict[item] = add_nested_dictionaries(
                    first_dict[item], second_dict[item]
                )
            else:
                merged_dict[item] = first_dict[item] + second_dict[item]
        else:
            merged_dict[item] = copy.deepcopy(first_dict[item])

    for item in second_dict:
        if item not in first_dict:
            merged_dict[item] = copy.deepcopy(second_dict[item])

    return merged_dict


def biased_skew(df_series):
    """
    Calculate the biased estimator for skewness of the given data.

    The definition is formalized as g_1 here:
        https://en.wikipedia.org/wiki/Skewness#Sample_skewness
    :param df_series: data to get skewness of, assuming floats
    :type df_series: pandas Series
    :return: biased skewness
    :rtype: float
    """
    n = len(df_series)
    if n < 1:
        return np.nan

    mean = sum(df_series) / n
    if np.isinf(mean) or np.isnan(mean):
        return np.nan

    diffs = df_series - mean
    squared_diffs = diffs**2
    cubed_diffs = squared_diffs * diffs
    M2 = sum(squared_diffs)
    M3 = sum(cubed_diffs)
    # This correction comes from the pandas implementation of
    # skewness, which zeroes these values out before computation
    # due to possible floating point errors that can occur.
    M2 = 0 if np.abs(M2) < 1e-14 else M2
    M3 = 0 if np.abs(M3) < 1e-14 else M3

    if M2 == 0:
        return 0.0

    with np.errstate(all="ignore"):
        skew = np.sqrt(n) * M3 / np.power(M2, 1.5)
    return skew


def biased_kurt(df_series):
    """
    Calculate the biased estimator for kurtosis of the given data.

    The definition is formalized as g_2 here:
        https://en.wikipedia.org/wiki/Kurtosis#A_natural_but_biased_estimator
    :param df_series: data to get kurtosis of, assuming floats
    :type df_series: pandas Series
    :return: biased kurtosis
    :rtype: float
    """
    n = len(df_series)
    if n < 1:
        return np.nan

    mean = sum(df_series) / n
    if np.isinf(mean) or np.isnan(mean):
        return np.nan

    diffs = df_series - mean
    squared_diffs = diffs**2
    fourth_diffs = squared_diffs * squared_diffs
    M2 = sum(squared_diffs)
    M4 = sum(fourth_diffs)
    # This correction comes from the pandas implementation of
    # kurtosis, which zeroes these values out before computation
    # due to possible floating point errors that can occur.
    M2 = 0 if np.abs(M2) < 1e-14 else M2
    M4 = 0 if np.abs(M4) < 1e-14 else M4

    if M2 == 0:
        return -3.0

    with np.errstate(all="ignore"):
        kurt = n * M4 / np.power(M2, 2) - 3
    return kurt


def find_diff_of_numbers(stat1, stat2):
    """
    Find the difference between two stats.

    If there is no difference, return "unchanged".
    For ints/floats, returns stat1 - stat2.

    :param stat1: the first statistical input
    :type stat1: Union[int, float]
    :param stat2: the second statistical input
    :type stat2: Union[int, float]
    :return: the difference of the stats
    """
    diff = "unchanged"
    if stat1 is None and stat2 is None:
        pass
    elif stat1 is None or stat2 is None:
        diff = [stat1, stat2]
    elif stat1 != stat2:
        diff = stat1 - stat2
    return diff


def find_diff_of_strings_and_bools(stat1, stat2):
    """
    Find the difference between two stats.

    If there is no difference, return "unchanged".
    For strings and bools, return list containing [stat1, stat2].

    :param stat1: the first statistical input
    :type stat1: Union[str, bool]
    :param stat2: the second statistical input
    :type stat2: Union[str, bool]
    :return: the difference of the stats
    """
    diff = "unchanged"
    if stat1 != stat2:
        diff = [stat1, stat2]
    return diff


def find_diff_of_lists_and_sets(stat1, stat2):
    """
    Find the difference between two stats.

    If there is no difference, return
    "unchanged". Remove duplicates and returns [unique values of stat1,
    shared values, unique values of stat2].

    :param stat1: the first statistical input
    :type stat1: Union[list, set]
    :param stat2: the second statistical input
    :type stat2: Union[list, set]
    :return: the difference of the stats
    """
    diff = "unchanged"
    if stat1 is None and stat2 is None:
        pass
    elif stat1 is None or stat2 is None:
        diff = [stat1, stat2]
    elif set(stat1) != set(stat2):
        unique1 = [element for element in stat1 if element not in stat2]
        shared = [element for element in stat1 if element in stat2]
        unique2 = [element for element in stat2 if element not in stat1]
        diff = [unique1, shared, unique2]
    return diff


def find_diff_of_dates(stat1, stat2):
    """
    Find the difference between two dates.

    If there is no difference, return
    "unchanged". For dates, return the difference in time.

    Because only days can be stored as negative values internally
    for timedelta objects, the output for these negative values is
    less readable due to the combination of signs in the default
    output. This returns a readable output for timedelta that
    accounts for potential negative differences.

    :param stat1: the first statistical input
    :type stat1: datetime.datetime object
    :param stat2: the second statistical input
    :type stat2: datetime.datetime object
    :return: Difference in stats
    :rtype: str
    """
    # We can use find_diff_of_numbers since datetime objects
    # can be compared and subtracted naturally
    diff = find_diff_of_numbers(stat1, stat2)
    if isinstance(diff, str):
        return diff
    if isinstance(diff, list):
        # Deal with the case where one stat is None
        return [None if i is None else i.strftime("%x %X") for i in diff]

    # Must be timedelta object
    if diff.days >= 0:
        return "+" + str(diff)

    return "-" + str(abs(diff))


def find_diff_of_dicts(dict1, dict2):
    """
    Find the difference between two dicts.

    For each key in each dict,
    return "unchanged" if there's no difference, otherwise return
    the difference. Assume that if the two dictionaries share the
    same key, their values are the same type.

    :param dict1: the first dict
    :type dict1: dict
    :param dict2: the second dict
    :type dict2: dict
    :return: Difference in the keys of each dict
    :rtype: dict
    """
    diff = {}
    for key, value1 in dict1.items():
        value2 = dict2.get(key, None)
        if isinstance(value1, list):
            diff[key] = find_diff_of_lists_and_sets(value1, value2)
        elif isinstance(value1, datetime.datetime):
            diff[key] = find_diff_of_dates(value1, value2)
        elif isinstance(value1, str):
            diff[key] = find_diff_of_strings_and_bools(value1, value2)
        else:
            diff[key] = find_diff_of_numbers(value1, value2)

    # Add any keys in dict2 that weren't in dict1
    for key, value in dict2.items():
        if key not in diff:
            diff[key] = [None, value]

    # If both dicts have no keys, it is unchanged
    if diff == {}:
        diff = "unchanged"

    return diff


def find_diff_of_matrices(matrix1, matrix2):
    """
    Find the difference between two matrices.

    :param matrix1: the first matrix
    :type matrix1: list(list(float))
    :param matrix2: the second matrix
    :type matrix2: list(list(float))
    :return: Difference in the matrix
    :rtype: list(list(float))
    """
    diff = None

    if matrix1 is not None and matrix2 is not None:
        mat1 = np.array(matrix1, dtype=np.float)
        mat2 = np.array(matrix2, dtype=np.float)

        if mat1.shape == mat2.shape:
            diff = mat1 - mat2
            if ((diff == 0) | np.isnan(diff)).all():
                diff = "unchanged"

    return diff


def find_diff_of_dicts_with_diff_keys(dict1, dict2):
    """
    Find the difference between two dicts.

    For each key in each dict,
    return "unchanged" if there's no difference, otherwise return
    the difference. Assume that if the two dictionaries share the
    same key, their values are the same type.

    :param dict1: the first dict
    :type dict1: dict
    :param dict2: the second dict
    :type dict2: dict
    :return: Difference in the keys of each dict
    :rtype: list
    """
    diff_1 = {}
    diff_shared = {}
    diff_2 = {}
    for key, value1 in dict1.items():
        if key in dict2:
            value2 = dict2[key]
            if isinstance(value1, list):
                diff_shared[key] = find_diff_of_lists_and_sets(value1, value2)
            elif isinstance(value1, datetime.datetime):
                diff_shared[key] = find_diff_of_dates(value1, value2)
            elif isinstance(value1, str) or isinstance(value1, bool):
                diff_shared[key] = find_diff_of_strings_and_bools(value1, value2)
            else:
                diff_shared[key] = find_diff_of_numbers(value1, value2)
        else:
            diff_1[key] = value1

    # Add any keys in dict2 that weren't in dict1
    for key, value in dict2.items():
        if key not in dict1:
            diff_2[key] = value

    diff = [diff_1, diff_shared, diff_2]

    # If both dicts have no keys, it is unchanged
    if diff == [{}, {}, {}]:
        diff = "unchanged"

    return diff


def get_memory_size(data, unit="M"):
    """
    Get memory size of the input data.

    :param data: list or array of data
    :type data: Union[list, numpy.array, pandas.DataFrame]
    :param unit: memory size unit (B, K, M, or G)
    :type unit: string
    :return: memory size of the input data
    """
    unit_map = collections.defaultdict(B=0, K=1, M=2, G=3)
    if unit not in unit_map:
        raise ValueError(
            "Currently only supports the "
            "memory size unit in {}".format(list(unit_map.keys()))
        )
    memory_size = 0
    for sentence in data:
        memory_size += len(sentence.encode("utf-8"))
    memory_size /= 1024.0 ** unit_map[unit]  # Conversion based on unit_map
    return memory_size


def method_timeit(method=None, name=None):
    """
    Measure execution time of provided method.

    Record time into times dictionary.

    :param method: method to time
    :type method: Callable
    :param name: key argument for the times dictionary
    :type name: str
    """

    def decorator(method, name_dec=None):
        @functools.wraps(method)
        def wrapper(self, *args, **kw):
            # necessary bc can't reassign external name
            name_dec = name
            if not name_dec:
                name_dec = method.__name__
            ts = time.time()
            result = method(self, *args, **kw)
            te = time.time()
            self.times[name_dec] += te - ts
            return result

        return wrapper

    if callable(method):
        return decorator(method, name_dec=name)
    return decorator


def perform_chi_squared_test_for_homogeneity(
    categories1, sample_size1, categories2, sample_size2
):
    """
    Perform a Chi Squared test for homogeneity between two groups.

    :param categories1: Categories and respective counts of the first group
    :type categories1: dict
    :param sample_size1: Number of samples in first group
    :type sample_size1: int
    :param categories2: Categories and respective counts of the second group
    :type categories2: dict
    :param sample_size2: Number of samples in second group
    :type sample_size2: int
    :return: Results of the chi squared test
    :rtype: dict
    """
    results = {"chi2-statistic": None, "df": None, "p-value": None}

    cat_counts = add_nested_dictionaries(categories1, categories2)

    # If one or less categories, we have zero/negative degrees of freedom,
    # which is not an appropriate value for this context
    num_cats = len(cat_counts)
    if len(cat_counts) <= 1:
        warnings.warn(
            "Insufficient number of categories. "
            "Chi-squared test cannot be performed.",
            RuntimeWarning,
        )
        return results

    # Calculate degrees of freedom
    # df = (rows - 1) * (cols - 1), in the case of two groups reduces to cols - 1
    df = num_cats - 1
    results["df"] = df

    total = sample_size1 + sample_size2

    # If a zero is found in either row or col sums, then an expected count will be
    # zero. This means the chi2-statistic and p-value are infinity and zero,
    # so calculation can be skipped.
    if 0 in [sample_size1, sample_size2] or 0 in cat_counts.values():
        results["chi2-statistic"] = np.inf
        results["p-value"] = 0
        return results

    # Calculate chi-sq statistic
    chi2_statistic = 0
    for cat, count in cat_counts.items():
        expected1 = sample_size1 * count / total
        expected2 = sample_size2 * count / total
        chi2_statistic += (categories1.get(cat, 0) - expected1) ** 2 / expected1
        chi2_statistic += (categories2.get(cat, 0) - expected2) ** 2 / expected2
    results["chi2-statistic"] = chi2_statistic

    # Calculate p-value, i.e. P(X > chi2_statistic)
    p_value = 1 - scipy.stats.chi2(df).cdf(chi2_statistic)
    results["p-value"] = p_value

    return results


def chunk(it, size):
    """
    Chunk things out.

    :param top_profile: Categories and respective counts of the second group
    :type top_profile: list
    :param other_profile: Number of samples in second group
    :type other_profile: int
    :return: Merge of two profile objects
    :rtype: Profile
    """
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def merge(top_profile, other_profile=None):
    """
    Merge two Profiles.

    :param top_profile: Categories and respective counts of the second group
    :type top_profile: list
    :param other_profile: Number of samples in second group
    :type other_profile: int
    :return: Merge of two profile objects
    :rtype: Profile
    """
    if not other_profile:
        return top_profile
    return top_profile + other_profile


def merge_profile_list(list_of_profiles, pool_count=5):
    """Merge list of profiles into a single profile.

    :param list_of_profiles: Categories and respective counts of the second group
    :type list_of_profiles: list
    :param pool_count: Number of samples in second group
    :type pool_count: int
    :return: Single profile that is the merge of all profiles in the
        `list_of_profiles` list.
    :rtype: Profile
    """
    # remove the labeler model from the first profile object
    # assuming that the labeler models are all the same across each profile
    # in the list
    for profile_idx, profile in enumerate(list_of_profiles):
        data_labeler = list_of_profiles[profile_idx]._remove_data_labelers()

    while len(list_of_profiles) > 1:
        list_of_profiles = chunk(list_of_profiles, 2)
        with mp.Pool(pool_count) as p:
            list_of_profiles = p.starmap(merge, list_of_profiles)

    list_of_profiles[0]._restore_data_labelers(data_labeler)
    return list_of_profiles[0]
