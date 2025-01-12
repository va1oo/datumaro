# Copyright (C) 2019-2022 Intel Corporation
# Copyright (C) 2023-2024 CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import os
import os.path as osp
import re
from functools import cached_property
from itertools import cycle
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, Union

import cv2
import numpy as np
import yaml

from datumaro.components.annotation import (
    Annotation,
    AnnotationType,
    Bbox,
    Label,
    LabelCategories,
    Points,
    PointsCategories,
    Polygon,
    Skeleton,
)
from datumaro.components.errors import (
    DatasetImportError,
    InvalidAnnotationError,
    UndeclaredLabelError,
)
from datumaro.components.extractor import CategoriesInfo, DatasetItem, Extractor, SourceExtractor
from datumaro.components.media import Image
from datumaro.util import parse_json_file, take_by
from datumaro.util.image import (
    DEFAULT_IMAGE_META_FILE_NAME,
    ImageMeta,
    find_images,
    load_image,
    load_image_meta_file,
)
from datumaro.util.meta_file_util import get_meta_file, has_meta_file, parse_meta_file
from datumaro.util.os_util import split_path

from .format import YoloPath, YOLOv8ClassificationFormat, YOLOv8Path, YOLOv8PoseFormat

T = TypeVar("T")


class YoloBaseExtractor(SourceExtractor):
    class Subset(Extractor):
        def __init__(self, name: str, parent: YoloBaseExtractor):
            super().__init__()
            self._name = name
            self._parent = parent
            self.items: Dict[str, Union[str, DatasetItem]] = {}

        def __iter__(self):
            for item_id in self.items:
                item = self._parent._get(item_id, self._name)
                if item is not None:
                    yield item

        def __len__(self):
            return len(self.items)

        def categories(self):
            return self._parent.categories()

    def __init__(
        self,
        rootpath: str,
        image_info: Union[None, str, ImageMeta] = None,
        **kwargs,
    ) -> None:
        if not osp.isdir(rootpath):
            raise DatasetImportError(f"Can't read dataset folder '{rootpath}'")

        super().__init__(**kwargs)

        self._path = rootpath

        assert image_info is None or isinstance(image_info, (str, dict))
        if image_info is None:
            image_info = osp.join(rootpath, DEFAULT_IMAGE_META_FILE_NAME)
            if not osp.isfile(image_info):
                image_info = {}
        if isinstance(image_info, str):
            image_info = load_image_meta_file(image_info)

        self._image_info = image_info

        self._categories = self._load_categories()

        self._subsets: Dict[str, YoloBaseExtractor.Subset] = {}

        for subset_name in self._get_subset_names():
            subset = YoloBaseExtractor.Subset(subset_name, self)
            subset.items = self._get_lazy_subset_items(subset_name)
            self._subsets[subset_name] = subset

    @classmethod
    def _image_loader(cls, *args, **kwargs):
        return load_image(*args, **kwargs, keep_exif=True)

    def _get(self, item_id: str, subset_name: str) -> Optional[DatasetItem]:
        subset = self._subsets[subset_name]
        item = subset.items[item_id]

        if isinstance(item, str):
            try:
                image_size = self._image_info.get(item_id)
                image_path = osp.join(self._path, item)

                if image_size:
                    image = Image(path=image_path, size=image_size)
                else:
                    image = Image(path=image_path, data=self._image_loader)

                annotations = self._parse_annotations(image, item_id=(item_id, subset_name))

                item = DatasetItem(
                    id=item_id, subset=subset_name, media=image, annotations=annotations
                )
                subset.items[item_id] = item
            except (FileNotFoundError, IOError, DatasetImportError) as e:
                self._ctx.error_policy.report_item_error(e, item_id=(item_id, subset_name))
                subset.items.pop(item_id)
                item = None

        return item

    def _get_subset_names(self):
        raise NotImplementedError()

    def _get_lazy_subset_items(self, subset_name: str):
        raise NotImplementedError()

    def _parse_annotations(self, image: Image, *, item_id: Tuple[str, str]) -> List[Annotation]:
        raise NotImplementedError()

    def _load_categories(self) -> CategoriesInfo:
        raise NotImplementedError()

    def __iter__(self):
        subsets = self._subsets
        pbars = self._ctx.progress_reporter.split(len(subsets))
        for pbar, (subset_name, subset) in zip(pbars, subsets.items()):
            for item in pbar.iter(subset, desc=f"Parsing '{subset_name}'"):
                yield item

    def __len__(self):
        return sum(len(s) for s in self._subsets.values())

    def get_subset(self, name):
        return self._subsets[name]


class YoloExtractor(YoloBaseExtractor):
    RESERVED_CONFIG_KEYS = YoloPath.RESERVED_CONFIG_KEYS

    def __init__(
        self,
        config_path: str,
        image_info: Union[None, str, ImageMeta] = None,
        **kwargs,
    ) -> None:
        if not osp.isfile(config_path):
            raise DatasetImportError(f"Can't read dataset descriptor file '{config_path}'")

        self._config_path = config_path
        super().__init__(rootpath=osp.dirname(config_path), image_info=image_info, **kwargs)

    def _get_subset_names(self):
        # The original format is like this:
        #
        # classes = 2
        # train  = data/train.txt
        # valid  = data/test.txt
        # names = data/obj.names
        # backup = backup/
        #
        # To support more subset names, we disallow subsets
        # called 'classes', 'names' and 'backup'.
        return [k for k in self._config if k not in self.RESERVED_CONFIG_KEYS]

    def _get_lazy_subset_items(self, subset_name: str):
        return {
            self.name_from_path(p): self.localize_path(p)
            for p in self._get_subset_image_paths(subset_name)
        }

    def _get_subset_image_paths(self, subset_name: str):
        list_path = osp.join(self._path, self.localize_path(self._config[subset_name]))
        if not osp.isfile(list_path):
            raise InvalidAnnotationError(f"Can't find '{subset_name}' subset list file")

        with open(list_path, "r", encoding="utf-8") as f:
            yield from (p for p in f if p.strip())

    @cached_property
    def _config(self) -> Dict[str, str]:
        with open(self._config_path, "r", encoding="utf-8") as f:
            config_lines = f.readlines()

        config = {}

        for line in config_lines:
            match = re.match(r"^\s*(\w+)\s*=\s*(.+)$", line)
            if not match:
                continue

            key = match.group(1)
            value = match.group(2)
            config[key] = value

        return config

    @staticmethod
    def localize_path(path: str) -> str:
        """
        Removes the "data/" prefix from the path
        """

        path = osp.normpath(path.strip()).replace("\\", "/")
        default_base = "data/"
        if path.startswith(default_base):
            path = path[len(default_base) :]
        return path

    @classmethod
    def name_from_path(cls, path: str) -> str:
        """
        Obtains <image name> from the path like [data/]<subset>_obj/<image_name>.ext

        <image name> can be <a/b/c/filename>, so it is
        more involved than just calling "basename()".
        """

        path = cls.localize_path(path)

        parts = split_path(path)
        if 1 < len(parts) and not osp.isabs(path):
            path = osp.join(*parts[1:])  # pylint: disable=no-value-for-parameter

        return osp.splitext(path)[0]

    def _get_labels_path_from_image_path(self, image_path: str) -> str:
        return osp.splitext(image_path)[0] + YoloPath.LABELS_EXT

    @staticmethod
    def _parse_field(value: str, cls: Type[T], field_name: str) -> T:
        try:
            return cls(value)
        except Exception as e:
            raise InvalidAnnotationError(
                f"Can't parse {field_name} from '{value}'. Expected {cls}"
            ) from e

    def _parse_annotations(self, image: Image, *, item_id: Tuple[str, str]) -> List[Annotation]:
        anno_path = self._get_labels_path_from_image_path(image.path)
        lines = []
        with open(anno_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(line)

        annotations = []

        if lines:
            # Use image info as late as possible to avoid unnecessary image loading
            if image.size is None:
                raise DatasetImportError(
                    f"Can't find image info for '{self.localize_path(image.path)}'"
                )
            image_height, image_width = image.size

        for line in lines:
            try:
                annotations.append(
                    self._load_one_annotation(line.split(), image_height, image_width)
                )
            except Exception as e:
                self._ctx.error_policy.report_annotation_error(e, item_id=item_id)

        return annotations

    def _map_label_id(self, label_id: str) -> int:
        label_id = self._parse_field(label_id, int, "bbox label id")
        if label_id not in self._categories[AnnotationType.label]:
            raise UndeclaredLabelError(str(label_id))
        return label_id

    def _load_one_annotation(
        self, parts: List[str], image_height: int, image_width: int
    ) -> Annotation:
        if len(parts) != 5:
            raise InvalidAnnotationError(
                f"Unexpected field count {len(parts)} in the bbox description. "
                "Expected 5 fields (label, xc, yc, w, h)."
            )
        label_id, xc, yc, w, h = parts

        label_id = self._map_label_id(label_id)

        w = self._parse_field(w, float, "bbox width")
        h = self._parse_field(h, float, "bbox height")
        x = self._parse_field(xc, float, "bbox center x") - w * 0.5
        y = self._parse_field(yc, float, "bbox center y") - h * 0.5

        return Bbox(
            x * image_width,
            y * image_height,
            w * image_width,
            h * image_height,
            label=label_id,
        )

    def _load_categories(self) -> CategoriesInfo:
        names_path = self._config.get("names")
        if not names_path:
            raise InvalidAnnotationError(f"Failed to parse names file path from config")

        names_path = osp.join(self._path, self.localize_path(names_path))

        if has_meta_file(osp.dirname(names_path)):
            return {
                AnnotationType.label: LabelCategories.from_iterable(
                    parse_meta_file(osp.dirname(names_path)).keys()
                )
            }

        label_categories = LabelCategories()

        with open(names_path, "r", encoding="utf-8") as f:
            for label in f:
                label = label.strip()
                if label:
                    label_categories.add(label)

        return {AnnotationType.label: label_categories}


class YOLOv8DetectionExtractor(YoloExtractor):
    RESERVED_CONFIG_KEYS = YOLOv8Path.RESERVED_CONFIG_KEYS

    def __init__(
        self,
        *args,
        config_file=None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

    def _parse_annotations(self, image: Image, *, item_id: Tuple[str, str]) -> List[Annotation]:
        anno_path = self._get_labels_path_from_image_path(image.path)
        if not osp.exists(anno_path):
            return []
        return super()._parse_annotations(image, item_id=item_id)

    @cached_property
    def _config(self) -> Dict[str, Any]:
        with open(self._config_path) as stream:
            try:
                return yaml.safe_load(stream)
            except yaml.YAMLError:
                raise InvalidAnnotationError("Failed to parse config file")

    @cached_property
    def _label_mapping(self) -> Dict[int, int]:
        names = self._config["names"]
        if isinstance(names, list):
            return {index: index for index in range(len(names))}
        if isinstance(names, dict):
            return {names_key: index for index, names_key in enumerate(sorted(names.keys()))}
        raise InvalidAnnotationError("Failed to parse names from config")

    def _map_label_id(self, ann_label_id: str) -> int:
        names = self._config["names"]
        ann_label_id = self._parse_field(ann_label_id, int, "label id")
        if isinstance(names, list):
            if ann_label_id < 0 or ann_label_id >= len(names):
                raise UndeclaredLabelError(str(ann_label_id))
            return ann_label_id

        if isinstance(names, dict):
            if ann_label_id not in names:
                raise UndeclaredLabelError(str(ann_label_id))
            return self._label_mapping[ann_label_id]

    def _load_names_from_config_file(self) -> list:
        names = self._config["names"]
        if isinstance(names, dict):
            names_with_mapped_keys = {
                self._label_mapping[names_key]: names[names_key] for names_key in names
            }
            return [names_with_mapped_keys[i] for i in range(len(names))]
        elif isinstance(names, list):
            return names
        raise InvalidAnnotationError("Failed to parse names from config")

    def _load_categories(self) -> CategoriesInfo:
        if has_meta_file(self._path):
            return {
                AnnotationType.label: LabelCategories.from_iterable(
                    parse_meta_file(self._path).keys()
                )
            }

        names = self._load_names_from_config_file()
        return {AnnotationType.label: LabelCategories.from_iterable(names)}

    def _get_labels_path_from_image_path(self, image_path: str) -> str:
        relative_image_path = osp.relpath(
            image_path, osp.join(self._path, YOLOv8Path.IMAGES_FOLDER_NAME)
        )
        relative_labels_path = osp.splitext(relative_image_path)[0] + YOLOv8Path.LABELS_EXT
        return osp.join(self._path, YOLOv8Path.LABELS_FOLDER_NAME, relative_labels_path)

    @classmethod
    def name_from_path(cls, path: str) -> str:
        """
        Obtains <image name> from the path like [data/]images/<subset>/<image_name>.ext

        <image name> can be <a/b/c/filename>, so it is
        more involved than just calling "basename()".
        """
        path = cls.localize_path(path)

        parts = split_path(path)
        if 2 < len(parts) and not osp.isabs(path):
            path = osp.join(*parts[2:])  # pylint: disable=no-value-for-parameter
        return osp.splitext(path)[0]

    def _get_subset_image_paths(self, subset_name: str):
        subset_images_source = self._config[subset_name]
        if isinstance(subset_images_source, str):
            if subset_images_source.endswith(YoloPath.SUBSET_LIST_EXT):
                yield from super()._get_subset_image_paths(subset_name)
            else:
                path = osp.join(self._path, self.localize_path(subset_images_source))
                if not osp.isdir(path):
                    raise InvalidAnnotationError(f"Can't find '{subset_name}' subset image folder")
                yield from (
                    osp.relpath(osp.join(root, file), self._path)
                    for root, dirs, files in os.walk(path)
                    for file in files
                    if osp.isfile(osp.join(root, file))
                )
        else:
            yield from subset_images_source


class YOLOv8SegmentationExtractor(YOLOv8DetectionExtractor):
    def _load_segmentation_annotation(
        self, parts: List[str], image_height: int, image_width: int
    ) -> Polygon:
        label_id = self._map_label_id(parts[0])
        points = [
            self._parse_field(
                value, float, f"polygon point {idx // 2} {'x' if idx % 2 == 0 else 'y'}"
            )
            for idx, value in enumerate(parts[1:])
        ]
        scaled_points = [
            value * size for value, size in zip(points, cycle((image_width, image_height)))
        ]
        return Polygon(scaled_points, label=label_id)

    def _load_one_annotation(
        self, parts: List[str], image_height: int, image_width: int
    ) -> Annotation:
        if len(parts) > 5 and len(parts) % 2 == 1:
            return self._load_segmentation_annotation(parts, image_height, image_width)
        raise InvalidAnnotationError(
            f"Unexpected field count {len(parts)} in the polygon description. "
            "Expected odd number > 5 of fields for segment annotation (label, x1, y1, x2, y2, x3, y3, ...)"
        )


class YOLOv8OrientedBoxesExtractor(YOLOv8DetectionExtractor):
    def _load_one_annotation(
        self, parts: List[str], image_height: int, image_width: int
    ) -> Annotation:
        if len(parts) != 9:
            raise InvalidAnnotationError(
                f"Unexpected field count {len(parts)} in the bbox description. "
                "Expected 9 fields (label, x1, y1, x2, y2, x3, y3, x4, y4)."
            )
        label_id = self._map_label_id(parts[0])
        points = [
            (
                self._parse_field(x, float, f"bbox point {idx} x") * image_width,
                self._parse_field(y, float, f"bbox point {idx} y") * image_height,
            )
            for idx, (x, y) in enumerate(take_by(parts[1:], 2))
        ]

        (center_x, center_y), (width, height), rotation = cv2.minAreaRect(
            np.array(points, dtype=np.float32)
        )
        rotation = rotation % 180

        return Bbox(
            x=center_x - width / 2,
            y=center_y - height / 2,
            w=width,
            h=height,
            label=label_id,
            attributes=(dict(rotation=rotation) if abs(rotation) > 0.00001 else {}),
        )


class YOLOv8PoseExtractor(YOLOv8DetectionExtractor):
    def __init__(
        self,
        *args,
        skeleton_sub_labels: Optional[Dict[str, List[str]]] = None,
        **kwargs,
    ) -> None:
        self._skeleton_sub_labels = skeleton_sub_labels
        super().__init__(*args, **kwargs)

    @cached_property
    def _kpt_shape(self) -> list[int]:
        if YOLOv8PoseFormat.KPT_SHAPE_FIELD_NAME not in self._config:
            raise InvalidAnnotationError(
                f"Failed to parse {YOLOv8PoseFormat.KPT_SHAPE_FIELD_NAME} from config"
            )
        kpt_shape = self._config[YOLOv8PoseFormat.KPT_SHAPE_FIELD_NAME]
        if not isinstance(kpt_shape, list) or len(kpt_shape) != 2:
            raise InvalidAnnotationError(
                f"Failed to parse {YOLOv8PoseFormat.KPT_SHAPE_FIELD_NAME} from config"
            )
        if kpt_shape[1] not in [2, 3]:
            raise InvalidAnnotationError(
                f"Unexpected values per point {kpt_shape[1]} in field"
                f"{YOLOv8PoseFormat.KPT_SHAPE_FIELD_NAME}. Expected 2 or 3."
            )
        if not isinstance(kpt_shape[0], int) or kpt_shape[0] < 0:
            raise InvalidAnnotationError(
                f"Unexpected number of points {kpt_shape[0]} in field "
                f"{YOLOv8PoseFormat.KPT_SHAPE_FIELD_NAME}. Expected non-negative integer."
            )

        return kpt_shape

    @cached_property
    def _skeleton_id_to_label_id(self) -> Dict[int, int]:
        point_categories = self._categories.get(
            AnnotationType.points, PointsCategories.from_iterable([])
        )
        return {index: label_id for index, label_id in enumerate(sorted(point_categories.items))}

    def _load_categories_from_meta_file(self) -> CategoriesInfo:
        dataset_meta = parse_json_file(get_meta_file(self._path))
        point_categories = PointsCategories.from_iterable(dataset_meta.get("point_categories", []))
        categories = {
            AnnotationType.label: LabelCategories.from_iterable(dataset_meta["label_categories"])
        }
        if len(point_categories) > 0:
            categories[AnnotationType.points] = point_categories
        return categories

    def _load_categories(self) -> CategoriesInfo:
        if "names" not in self._config:
            raise InvalidAnnotationError(f"Failed to parse names from config")

        if has_meta_file(self._path):
            return self._load_categories_from_meta_file()

        max_number_of_points, _ = self._kpt_shape
        skeleton_labels = self._load_names_from_config_file()

        if self._skeleton_sub_labels:
            if missing_labels := set(skeleton_labels) - set(self._skeleton_sub_labels):
                raise InvalidAnnotationError(
                    f"Labels from config file are absent in sub label hint: {missing_labels}"
                )

            if skeletons_with_wrong_sub_labels := [
                skeleton
                for skeleton in skeleton_labels
                if len(self._skeleton_sub_labels[skeleton]) > max_number_of_points
            ]:
                raise InvalidAnnotationError(
                    f"Number of points in skeletons according to config file is {max_number_of_points}. "
                    f"Following skeletons have more sub labels: {skeletons_with_wrong_sub_labels}"
                )

        children_labels = self._skeleton_sub_labels or {
            skeleton_label: [
                f"{skeleton_label}_point_{point_index}"
                for point_index in range(max_number_of_points)
            ]
            for skeleton_label in skeleton_labels
        }

        point_labels = [
            (child_name, skeleton_label)
            for skeleton_label in skeleton_labels
            for child_name in children_labels[skeleton_label]
        ]

        point_categories = PointsCategories.from_iterable(
            [
                (index, children_labels[skeleton_label], set())
                for index, skeleton_label in enumerate(skeleton_labels)
            ]
        )
        categories = {
            AnnotationType.label: LabelCategories.from_iterable(skeleton_labels + point_labels)
        }
        if len(point_categories) > 0:
            categories[AnnotationType.points] = point_categories

        return categories

    def _map_label_id(self, ann_label_id: str) -> int:
        skeleton_id = super()._map_label_id(ann_label_id)
        return self._skeleton_id_to_label_id[skeleton_id]

    def _load_one_annotation(
        self, parts: List[str], image_height: int, image_width: int
    ) -> Annotation:
        max_number_of_points, values_per_point = self._kpt_shape
        if len(parts) != 5 + max_number_of_points * values_per_point:
            raise InvalidAnnotationError(
                f"Unexpected field count {len(parts)} in the skeleton description. "
                "Expected 5 fields (label, xc, yc, w, h)"
                f"and then {values_per_point} for each of {max_number_of_points} points"
            )

        label_id = self._map_label_id(parts[0])

        point_labels = self._categories[AnnotationType.points][label_id].labels
        point_label_ids = [
            self._categories[AnnotationType.label].find(
                name=point_label,
                parent=self._categories[AnnotationType.label][label_id].name,
            )[0]
            for point_label in point_labels
        ]

        points = [
            Points(
                [
                    self._parse_field(parts[x_index], float, f"skeleton point {point_index} x")
                    * image_width,
                    self._parse_field(parts[y_index], float, f"skeleton point {point_index} y")
                    * image_height,
                ],
                (
                    [
                        self._parse_field(
                            parts[visibility_index],
                            int,
                            f"skeleton point {point_index} visibility",
                        ),
                    ]
                    if values_per_point == 3
                    else [Points.Visibility.visible.value]
                ),
                label=label_id,
            )
            for point_index, label_id in enumerate(point_label_ids)
            for x_index, y_index, visibility_index in [
                (
                    5 + point_index * values_per_point,
                    5 + point_index * values_per_point + 1,
                    5 + point_index * values_per_point + 2,
                ),
            ]
        ]
        return Skeleton(points, label=label_id)


class YOLOv8ClassificationExtractor(YoloBaseExtractor):
    def _get_subset_names(self):
        return [
            subset_name
            for subset_name in os.listdir(self._path)
            if osp.isdir(osp.join(self._path, subset_name))
        ]

    def _get_image_paths_for_subset_and_label(self, subset_name: str, label_name: str) -> list[str]:
        category_folder = osp.join(self._path, subset_name, label_name)
        image_list_path = osp.join(category_folder, YOLOv8ClassificationFormat.LABELS_FILE)
        if osp.isfile(image_list_path):
            with open(image_list_path, "r", encoding="utf-8") as f:
                yield from (osp.join(subset_name, label_name, line.strip()) for line in f)

        yield from (
            osp.relpath(image_path, self._path)
            for image_path in find_images(category_folder, recursive=True)
        )

    def _get_item_info_from_labels_file(self, subset_name: str) -> Optional[Dict]:
        subset_path = osp.join(self._path, subset_name)
        labels_file_path = osp.join(subset_path, YOLOv8ClassificationFormat.LABELS_FILE)
        if osp.isfile(labels_file_path):
            return parse_json_file(labels_file_path)

    def _get_lazy_subset_items(self, subset_name: str):
        subset_path = osp.join(self._path, subset_name)

        if item_info := self._get_item_info_from_labels_file(subset_name):
            return {id: osp.join(subset_name, item_info[id]["path"]) for id in item_info}

        return {
            self.name_from_path(image_path): image_path
            for category_name in os.listdir(subset_path)
            if osp.isdir(osp.join(subset_path, category_name))
            for image_path in self._get_image_paths_for_subset_and_label(subset_name, category_name)
        }

    def _parse_annotations(self, image: Image, *, item_id: Tuple[str, str]) -> List[Annotation]:
        item_id, subset_name = item_id
        if item_info := self._get_item_info_from_labels_file(subset_name):
            label_names = item_info[item_id]["labels"]
        else:
            subset_path = osp.join(self._path, subset_name)
            relative_image_path = osp.relpath(image.path, subset_path)
            label_names = [split_path(relative_image_path)[0]]

        return [
            Label(label=self._categories[AnnotationType.label].find(label)[0])
            for label in label_names
            if label != YOLOv8ClassificationFormat.IMAGE_DIR_NO_LABEL
        ]

    def _load_categories(self) -> CategoriesInfo:
        categories = set()
        for subset in os.listdir(self._path):
            subset_path = osp.join(self._path, subset)
            if not osp.isdir(subset_path):
                continue

            if item_info := self._get_item_info_from_labels_file(subset):
                categories.update(*[item_info[item_id]["labels"] for item_id in item_info])

            for label_dir_name in os.listdir(subset_path):
                if not osp.isdir(osp.join(subset_path, label_dir_name)):
                    continue
                if label_dir_name == YOLOv8ClassificationFormat.IMAGE_DIR_NO_LABEL:
                    continue
                categories.add(label_dir_name)
        return {AnnotationType.label: LabelCategories.from_iterable(sorted(categories))}

    @classmethod
    def name_from_path(cls, path_from_root: str) -> str:
        subset_folder = split_path(path_from_root)[0]
        path_from_subset_folder = osp.relpath(path_from_root, subset_folder)
        return osp.splitext(path_from_subset_folder)[0]
