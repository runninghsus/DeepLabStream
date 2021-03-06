"""
DeepLabStream
© J.Schweihoff, M. Loshakov
University Bonn Medical Faculty, Germany
https://github.com/SchwarzNeuroconLab/DeepLabStream
Licensed under GNU General Public License v3.0
"""

import sys
import os
import numpy as np
from itertools import product, combinations
from utils.analysis import calculate_distance
from utils.plotter import plot_special_peak
from skimage.feature import peak_local_max
from scipy.ndimage.measurements import label, maximum_position
from scipy.ndimage.morphology import generate_binary_structure, binary_erosion
from scipy.ndimage.filters import maximum_filter
from utils.configloader import deeplabcut_config

MODEL = deeplabcut_config['model']
DLC_PATH = deeplabcut_config['dlc_path']

# trying importing functions using deeplabcut module, if DLC 2 is installed correctly
try:
    import deeplabcut.pose_estimation_tensorflow.nnet.predict as predict
    from deeplabcut.pose_estimation_tensorflow.config import load_config
    models_folder = 'pose_estimation_tensorflow/models/'
# if not DLC 2 is not installed, try import from DLC 1 the old way
except ImportError:
    # adding DLC posing path and loading modules from it
    sys.path.insert(0, DLC_PATH + "/pose-tensorflow")
    from config import load_config
    from nnet import predict
    models_folder = 'pose-tensorflow/models/'


def load_deeplabcut():
    """
    Loads TensorFlow with predefined in config DeepLabCut model

    :return: tuple of DeepLabCut config, TensorFlow session, inputs and outputs
    """
    model = os.path.join(DLC_PATH, models_folder, MODEL)
    cfg = load_config(os.path.join(model, 'test/pose_cfg.yaml'))
    snapshots = sorted([sn.split('.')[0] for sn in os.listdir(model + '/train/') if "index" in sn])
    cfg['init_weights'] = model + '/train/' + snapshots[-1]

    sess, inputs, outputs = predict.setup_pose_prediction(cfg)
    return cfg, sess, inputs, outputs


def get_pose(image, config, session, inputs, outputs):
    """
    Gets scoremap, local reference and pose from DeepLabCut using given image
    Pose is most probable points for each joint, and not really used later
    Scoremap and local reference is essential to extract skeletons
    :param image: frame which would be analyzed
    :param config, session, inputs, outputs: DeepLabCut configuration and TensorFlow variables from load_deeplabcut()

    :return: tuple of scoremap, local reference and pose
    """
    scmap, locref, pose = predict.getpose(image, config, session, inputs, outputs, True)
    return scmap, locref, pose


def find_local_peaks_new(scoremap: np.ndarray, local_reference: np.ndarray, animal_number: int, config: dict) -> dict:
    """
    Function for finding local peaks for each joint on provided scoremap
    :param scoremap: scmap from get_pose function
    :param local_reference: locref from get_pose function
    :param animal_number: number of animals for which we need to find peaks, also used for critical joints
        Critical joint are used to define skeleton of an animal
        There can not be more than animal_number point for each critical joint
    :param config: DeepLabCut config from load_deeplabcut()

    :returns all_joints dictionary with coordinates as list of tuples for each joint
    """

    # loading animal joints from config
    all_joints_names = config['all_joints_names']
    # critical_joints = ['neck', 'tailroot']
    all_peaks = {}
    # loading stride from config
    stride = config['stride']
    # filtering scoremap
    scoremap[scoremap < 0.1] = 0
    plot_special_peak()
    for joint_num, joint in enumerate(all_joints_names):
        all_peaks[joint] = []
        # selecting the joint in scoremap and locref
        lr_joint = local_reference[:, :, joint_num]
        sm_joint = scoremap[:, :, joint_num]
        # applying maximum filter with footprint
        neighborhood = generate_binary_structure(2, 1)
        sm_max_filter = maximum_filter(sm_joint, footprint=neighborhood)
        # eroding filtered scoremap
        erosion_structure = generate_binary_structure(2, 3)
        sm_max_filter_eroded = binary_erosion(sm_max_filter, structure=erosion_structure).astype(sm_max_filter.dtype)
        # labeling eroded filtered scoremap
        labeled_sm_eroded, num = label(sm_max_filter_eroded)
        # if joint is 'critical' and we have too few labels then we try a workaround to ensure maximum found peaks
        # for all other joints - normal procedure with cutoff point at animal_number
        peaks = maximum_position(sm_joint, labels=labeled_sm_eroded, index=range(1, num + 1))
        if num != animal_number:
            peaks = [tuple(peak) for peak in peak_local_max(sm_joint, min_distance=4, num_peaks=animal_number)]

        if len(peaks) > animal_number:
            peaks = peaks[:animal_number + 1]

        # using scoremap peaks to get the coordinates on original image
        for peak in peaks:
            offset = lr_joint[peak]
            prob = sm_joint[peak]  # not used
            # some weird DLC magic with stride and offsets
            coordinates = np.floor(np.array(peak)[::-1] * stride + 0.5 * stride + offset)
            all_peaks[joint].append([tuple(coordinates.astype(int)), joint])
    return all_peaks


def calculate_skeletons(peaks: dict, animals_number: int) -> list:
    """
    Creating skeletons from given peaks
    There could be no more skeletons than animals_number
    Only unique skeletons output
    """
    # creating a cartesian product out of all joints
    # this product contains all possible variations (dots clusters) of all joints groups
    cartesian_p = product(*peaks.values(), repeat=1)

    def calculate_closest_distances(dots_cluster: list) -> float:
        """
        Calculating a sum of all distances between all dots in a cluster
        """
        # extracting dots coordinates from given list
        dots_coordinates = (dot[0] for dot in dots_cluster)
        # calculating sum of each dots cluster
        product_sum = sum(calculate_distance(*c) for c in combinations(dots_coordinates, 2))
        return product_sum

    # sorting groups by their sum
    sorted_product = sorted(cartesian_p, key=lambda c: calculate_closest_distances(c), reverse=False)

    # creating skeletons from top dots cluster
    def compare_clusters(unique_clusters: list, new_cluster: tuple) -> bool:
        """
        Compare some new cluster against every existing unique cluster to find if it is unique
        :param unique_clusters: list of existing unique cluster
        :param new_cluster: cluster with same dots
        :return: if new cluster is unique
        """
        # compare each element of tuple for uniqueness
        # finding unique combinations of joints within all possible combinations
        compare = lambda cl1, cl2: not any([(s1 == s2) for s1, s2 in zip(cl1, cl2)])
        # create a uniqueness check list
        # so if a list consists at least one False then new_cluster is not unique
        comparison = [compare(u_cluster, new_cluster) for u_cluster in unique_clusters]
        return all(comparison)

    def create_animal_skeleton(dots_cluster: tuple) -> dict:
        """
        Creating a easy to read skeleton from dots cluster
        Format for each joint:
        {'joint_name': (x,y)}
        """
        skeleton = {}
        for dot in dots_cluster:
            skeleton[dot[-1]] = dot[0]
        return skeleton

    top_unique_clusters = []
    animal_skeletons = []

    if sorted_product:
        # add first cluster in our sorted list
        top_unique_clusters.append(sorted_product[0])
        for cluster in sorted_product[1:]:
            # check if cluster is unique and we have a room for it
            if compare_clusters(top_unique_clusters, cluster) and len(top_unique_clusters) < animals_number:
                top_unique_clusters.append(cluster)
            # there couldn't be more clusters then animal_number limit
            elif len(top_unique_clusters) == animals_number:
                break

    # creating a skeleton out of each cluster in our top clusters list
    for unique_cluster in top_unique_clusters:
        animal_skeletons.append(create_animal_skeleton(unique_cluster))

    return animal_skeletons
