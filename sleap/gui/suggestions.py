"""
Module for generating lists of suggested frames (for labeling or reviewing).
"""

import numpy as np
import itertools

from skimage.transform import rescale
from sklearn.decomposition import PCA
from sklearn.cluster import KMeans

import cv2

from typing import List, Tuple

from sleap.io.video import Video


class VideoFrameSuggestions:
    """
    Class for generating lists of suggested frames.

    Implements various algorithms as methods:
    * strides
    * random
    * pca_cluster
    * brisk
    * proofreading

    Each of algorithm method should accept `video`; other parameters will be
    passed from the `params` dict given to :meth:`suggest`.

    """

    rescale = True
    rescale_below = 512

    @classmethod
    def suggest(cls, video: Video, params: dict, labels: "Labels" = None) -> List[int]:
        """
        This is the main entry point for generating lists of suggested frames.

        Args:
            video: A `Video` object for which we're generating suggestions.
            params: A dictionary with all params to control how we generate
                suggestions, minimally this will have a "method" key with
                the name of one of the class methods.
            labels: A `Labels` object for which we are generating suggestions.

        Returns:
            List of suggested frame indices.
        """

        # map from method param value to corresponding class method
        method_functions = dict(
            strides=cls.strides,
            random=cls.random,
            pca=cls.pca_cluster,
            # hog=cls.hog,
            brisk=cls.brisk,
            proofreading=cls.proofreading,
        )

        method = params["method"]
        if method_functions.get(method, None) is not None:
            return method_functions[method](video=video, labels=labels, **params)
        else:
            print(f"No {method} method found for generating suggestions.")

    # Functions corresponding to "method" param

    @classmethod
    def strides(cls, video, per_video=20, **kwargs):
        """Method to generate suggestions by taking strides through video."""
        suggestions = list(range(0, video.frames, video.frames // per_video))
        suggestions = suggestions[:per_video]
        return suggestions

    @classmethod
    def random(cls, video, per_video=20, **kwargs):
        """Method to generate suggestions by taking random frames in video."""
        import random

        suggestions = random.sample(range(video.frames), per_video)
        return suggestions

    @classmethod
    def pca_cluster(cls, video, initial_samples, **kwargs):
        """Method to generate suggestions by using PCA clusters."""
        sample_step = video.frames // initial_samples
        feature_stack, frame_idx_map = cls.frame_feature_stack(video, sample_step)

        result = cls.feature_stack_to_suggestions(
            feature_stack, frame_idx_map, **kwargs
        )

        return result

    @classmethod
    def brisk(cls, video, initial_samples, **kwargs):
        """Method to generate suggestions using PCA on Brisk features."""
        sample_step = video.frames // initial_samples
        feature_stack, frame_idx_map = cls.brisk_feature_stack(video, sample_step)

        result = cls.feature_stack_to_suggestions(
            feature_stack, frame_idx_map, **kwargs
        )

        return result

    @classmethod
    def proofreading(
        cls, video: Video, labels: "Labels", score_limit, instance_limit, **kwargs
    ):
        """Method to generate suggestions for proofreading."""
        score_limit = float(score_limit)
        instance_limit = int(instance_limit)

        lfs = labels.find(video)

        frames = len(lfs)
        idxs = np.ndarray((frames), dtype="int")
        scores = np.full((frames, instance_limit), 100.0, dtype="float")

        # Build matrix with scores for instances in frames
        for i, lf in enumerate(lfs):
            # Scores from instances in frame
            frame_scores = [inst.score for inst in lf if hasattr(inst, "score")]
            # Just get the lowest scores
            if len(frame_scores) > instance_limit:
                frame_scores = sorted(frame_scores)[:instance_limit]
            # Add to matrix
            scores[i, : len(frame_scores)] = frame_scores
            idxs[i] = lf.frame_idx

        # Find instances below score of <score_limit>
        low_instances = np.nansum(scores < score_limit, axis=1)

        # Find all the frames with at least <instance_limit> low scoring instances
        result = idxs[low_instances >= instance_limit].tolist()

        return result

    # Functions for building "feature stack", the (samples * features) matrix
    # These are specific to the suggestion method

    @classmethod
    def frame_feature_stack(
        cls, video: Video, sample_step: int = 5
    ) -> Tuple[np.ndarray, List[int]]:
        """Generates matrix of sampled video frame images."""
        sample_count = video.frames // sample_step

        factor = cls.get_scale_factor(video)

        frame_idx_map = []
        flat_stack = []

        for i in range(sample_count):
            frame_idx = i * sample_step

            img = video[frame_idx].squeeze()
            multichannel = video.channels > 1
            img = rescale(img, scale=0.5, anti_aliasing=True, multichannel=multichannel)

            flat_img = img.flatten()

            flat_stack.append(flat_img)
            frame_idx_map.append(frame_idx)

        flat_stack = np.stack(flat_stack, axis=0)
        return (flat_stack, frame_idx_map)

    @classmethod
    def brisk_feature_stack(
        cls, video: Video, sample_step: int = 5
    ) -> Tuple[np.ndarray, List[int]]:
        """Generates Brisk features from sampled video frames."""
        brisk = cv2.BRISK_create()

        factor = cls.get_scale_factor(video)

        feature_stack = None
        frame_idx_map = []
        for frame_idx in range(0, video.frames, sample_step):
            img = video[frame_idx][0]
            img = cls.resize(img, factor)
            kps, descs = brisk.detectAndCompute(img, None)

            # img2 = cv2.drawKeypoints(img, kps, None, color=(0,255,0), flags=0)
            if descs is not None:
                if feature_stack is None:
                    feature_stack = descs
                else:
                    feature_stack = np.concatenate((feature_stack, descs))
                frame_idx_map.extend([frame_idx] * descs.shape[0])

        return (feature_stack, frame_idx_map)

    # Functions for making suggestions based on "feature stack"
    # These are common for all suggestion methods

    @staticmethod
    def to_frame_idx_list(
        selected_list: List[int], frame_idx_map: List[int]
    ) -> List[int]:
        """Convert list of row indexes to list of frame indexes."""
        return list(map(lambda x: frame_idx_map[x], selected_list))

    @classmethod
    def feature_stack_to_suggestions(
        cls,
        feature_stack: np.ndarray,
        frame_idx_map: List[int],
        return_clusters: bool = False,
        **kwargs,
    ) -> List[int]:
        """
        Turns a feature stack matrix into a list of suggested frames.

        Args:
            feature_stack: (n * features) matrix.
            frame_idx_map: List indexed by rows of feature stack which gives
                frame index for each row in feature_stack. This allows a
                single frame to correspond to multiple rows in feature_stack.
            return_clusters: Whether to return the intermediate result for
                debugging, i.e., a list that gives the list of suggested
                frames for each cluster.

        Returns:
            List of frame index suggestions.
        """

        selected_by_cluster = cls.feature_stack_to_clusters(
            feature_stack=feature_stack, frame_idx_map=frame_idx_map, **kwargs
        )

        if return_clusters:
            return selected_by_cluster

        selected_list = cls.clusters_to_list(
            selected_by_cluster=selected_by_cluster, **kwargs
        )

        return selected_list

    @classmethod
    def feature_stack_to_clusters(
        cls,
        feature_stack: np.ndarray,
        frame_idx_map: List[int],
        clusters: int = 5,
        per_cluster: int = 5,
        pca_components: int = 50,
        **kwargs,
    ) -> List[int]:
        """
        Runs PCA to generate clusters of frames based on given features.

        Args:
            feature_stack: (n * features) matrix.
            frame_idx_map: List indexed by rows of feature stack which gives
                frame index for each row in feature_stack. This allows a
                single frame to correspond to multiple rows in feature_stack.
            clusters: number of clusters
            per_cluster: How many suggestions (at most) to take from each
                cluster.
            pca_components: Number of PCA components, for reducing feature
                space before clustering

        Returns:
            A list of lists:
            * for each cluster, a list of frame indices.
        """

        stack_height = feature_stack.shape[0]

        # limit number of components by number of samples
        pca_components = min(pca_components, stack_height)

        # get features for each frame
        pca = PCA(n_components=pca_components)
        flat_small = pca.fit_transform(feature_stack)

        # cluster based on features
        kmeans = KMeans(n_clusters=clusters)
        row_labels = kmeans.fit_predict(flat_small)

        # take samples from each cluster
        selected_by_cluster = []
        selected_set = set()
        for i in range(clusters):
            cluster_items, = np.where(row_labels == i)

            # convert from row indexes to frame indexes
            cluster_items = cls.to_frame_idx_list(cluster_items, frame_idx_map)

            # remove items from cluster_items if they've already been sampled for another cluster
            # TODO: should this be controlled by a param?
            cluster_items = list(set(cluster_items) - selected_set)

            # pick [per_cluster] items from this cluster
            samples_from_bin = np.random.choice(
                cluster_items, min(len(cluster_items), per_cluster), False
            )
            samples_from_bin.sort()
            selected_by_cluster.append(samples_from_bin)
            selected_set = selected_set.union(set(samples_from_bin))

        return selected_by_cluster

    @classmethod
    def clusters_to_list(
        cls, selected_by_cluster: List[List[int]], interleave: bool = True, **kwargs
    ) -> list:
        """
        Merges per cluster suggestion lists into single list for entire video.

        Args:
            selected_by_cluster: The list of lists of row indexes.
            interleave: Whether to interleave suggestions from clusters.

        Returns:
            List of frame indices.
        """

        if interleave:
            # cycle clusters
            all_selected = itertools.chain.from_iterable(
                itertools.zip_longest(*selected_by_cluster)
            )
            # remove Nones and convert back to list
            all_selected = list(filter(lambda x: x is not None, all_selected))
        else:
            all_selected = list(itertools.chain.from_iterable(selected_by_cluster))
            all_selected.sort()

        all_selected = [int(x) for x in all_selected]

        return all_selected

    # Utility functions

    @classmethod
    def get_scale_factor(cls, video: "Video") -> int:
        """
        Determines how much we need to scale to get video within size.

        Size is specified by :attr:`rescale_below`.
        """
        factor = 1
        if cls.rescale:
            largest_dim = max(video.height, video.width)
            factor = 1
            while largest_dim / factor > cls.rescale_below:
                factor += 1
        return factor

    @staticmethod
    def resize(img: np.ndarray, factor: float) -> np.ndarray:
        """Resizes frame image by scaling factor."""
        h, w, _ = img.shape
        if factor != 1:
            return cv2.resize(img, (h // factor, w // factor))
        else:
            return img


if __name__ == "__main__":
    # load some images
    filename = "tests/data/videos/centered_pair_small.mp4"
    filename = "files/190605_1509_frame23000_24000.sf.mp4"
    video = Video.from_filename(filename)

    debug = False

    x = VideoFrameSuggestions.hog(
        video=video, sample_step=20, clusters=5, per_cluster=5, return_clusters=debug
    )
    print(x)

    if debug:

        import matplotlib.pyplot as plt

        rows = len(x)
        cols = max((len(r) for r in x))

        fig, ax = plt.subplots(rows, cols)
        for r in range(rows):
            for c in range(cols):
                if c < len(x[r]):
                    frame_idx = x[r][c]
                    frame = video[frame_idx]
                    ax[r, c].imshow(frame[0].squeeze(), cmap="gray")
                    ax[r, c].set_title(f"frame {frame_idx}")
                ax[r, c].axis("off")
        plt.show()

    # brisk = cv2.BRISK_create()

    # feature_stack = None
    # frame_idx_map = []
    # for frame_idx in range(1, video.frames, 200):
    #     img = video[frame_idx][0]

    #     kps, descs = brisk.detectAndCompute(img, None)

    #     # img2 = cv2.drawKeypoints(img, kps, None, color=(0,255,0), flags=0)

    #     if feature_stack is None:
    #         feature_stack = descs
    #     else:
    #         feature_stack = np.concatenate((feature_stack, descs))
    #     frame_idx_map.extend([frame_idx] * descs.shape[0])

    # print(f"final result: {feature_stack.shape}")
    # print(frame_idx_map)

# for a,b in zip(kps,descs):
#   print(f"{a}: {b}")
# print(len(kps))
# print(len(descs))

# foo = cv2.FeatureDetector_create("SIFT")
# surf = cv2.xfeatures2d.SURF_create(400)
# kp, des = surf.detectAndCompute(video[13], None)
# print(len(kp))
# print(des.shape)

# print(VideoFrameSuggestions.suggest(video, dict(method="pca")))