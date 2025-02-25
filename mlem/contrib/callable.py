import posixpath
from collections import defaultdict
from importlib import import_module
from io import BytesIO
from typing import Any, Callable, ClassVar, Dict, Optional, Tuple
from uuid import uuid4

from dill import Pickler, Unpickler

from mlem.core.artifacts import Artifacts, Storage
from mlem.core.hooks import LOW_PRIORITY_VALUE
from mlem.core.model import (
    ModelAnalyzer,
    ModelHook,
    ModelIO,
    ModelType,
    Signature,
    SimplePickleIO,
)

UUID_PREFIX = "_"


class PickleModelIO(ModelIO):
    """
    ModelIO for pickle-able models
    When model is dumped, recursively checks objects if they can be dumped with ModelIO instead of pickling
    So, if you use function that internally calls tensorflow model, this tensorflow model will be dumped with
    tensorflow code and not pickled
    """

    file_name: ClassVar[str] = "data.pkl"
    type: ClassVar[str] = "pickle"
    io_ext: ClassVar[str] = ".io"

    def dump(self, storage: Storage, path, model) -> Artifacts:
        model_blob, refs = self._serialize_model(model)
        arts = {}
        if len(refs) == 0:
            with storage.open(path) as (f, art):
                f.write(model_blob)
                return {self.file_name: art}

        with storage.open(posixpath.join(path, self.file_name)) as (f, art):
            f.write(model_blob)
            arts[self.file_name] = art

        for uuid, (io, obj) in refs.items():
            arts.update(
                {
                    f"{uuid}_{k}": v
                    for k, v in io.dump(
                        storage, posixpath.join(path, uuid), obj
                    ).items()
                }
            )
            with storage.open(posixpath.join(path, uuid + self.io_ext)) as (
                f,
                art,
            ):
                f.write(self._serialize_io(io))
                arts[uuid + self.io_ext] = art
        return arts

    def load(self, artifacts: Artifacts):
        refs = {}
        root = artifacts[self.file_name]
        if len(artifacts) > 1:
            ref_artifacts: Dict[str, Artifacts] = defaultdict(dict)
            ref_ios = {}

            for art_name, art in artifacts.items():
                if art == root:
                    continue

                if art_name.endswith(self.io_ext):
                    ref_uuid = art_name[: -len(self.io_ext)]
                    with art.open() as f:
                        ref_ios[ref_uuid] = self._deserialize_io(f)
                else:
                    ref_uuid, subname = art_name.split("_", maxsplit=1)
                    ref_artifacts[ref_uuid][subname] = art
            for uuid, io in ref_ios.items():
                refs[uuid] = io.load(ref_artifacts.get(uuid, {}))
        with root.open() as f:
            return self._deserialize_model(f, refs)

    @staticmethod
    def _serialize_model(
        model,
    ) -> Tuple[bytes, Dict[str, Tuple[ModelIO, Any]]]:
        """
        Helper method to pickle model and get payload and refs
        :return: tuple of payload and refs
        """
        f = BytesIO()
        pklr = _ModelPickler(model, f, recurse=True)
        pklr.dump(model)
        return f.getvalue(), pklr.refs

    @staticmethod
    def _deserialize_model(in_file, refs):
        """
        Helper method to unpickle model from payload and refs
        :param in_file: payload
        :param refs: refs
        :return: unpickled model
        """
        return _ModelUnpickler(refs, in_file).load()

    @staticmethod
    def _serialize_io(io):
        """
        Helper method to serialize object's IO as ref
        :param io: :class:`ModelIO` instance
        :return: ref payload
        """
        io_type = type(io)
        return f"{io_type.__module__}.{io_type.__name__}".encode("utf-8")

    @staticmethod
    def _deserialize_io(in_file):
        """
        Helper method to deserialize object's IO from ref payload
        :param in_file: ref payload
        :return: :class:`ModelIO` instance
        """
        io_type_full_name = in_file.read().decode("utf-8")
        *mod_name, type_name = io_type_full_name.split(".")
        mod_name, pkg_name = ".".join(mod_name), ".".join(mod_name[:-1])
        return import_module(mod_name, pkg_name).__dict__[type_name]()


class _ModelPickler(Pickler):
    """
    A class to pickle model with respect to model_types of inner objects
    :param model: model object to serialize
    :param args: dill.Pickler args
    :param kwargs: dill.Pickler kwargs
    """

    def __init__(self, model, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = model
        self.refs: Dict[str, Tuple[ModelIO, Any]] = {}

        known_types = set()
        for hook in ModelAnalyzer.hooks:
            if not isinstance(hook, CallableModelType) and hook.valid_types:
                known_types.update(hook.valid_types)
        self.known_types = tuple(known_types)

    def _get_non_pickle_io(self, obj):
        """
        Checks if obj has non-Pickle IO and returns it
        :param obj: object to check
        :return: non-Pickle :class:`ModelIO` instance or None
        """

        # avoid calling heavy analyzer machinery for "unknown" objects:
        # they are either non-models or callables
        if not isinstance(obj, self.known_types):
            return None

        # we couldn't import analyzer at top as it leads to circular import failure
        try:
            io = ModelAnalyzer.analyze(obj).io
            return (
                None if isinstance(io, (PickleModelIO, SimplePickleIO)) else io
            )
        except ValueError:
            # non-model object
            return None

    def persistent_id(self, obj: Any) -> Any:
        io = self._get_non_pickle_io(obj)
        if io is None:
            return None
        obj_uuid = str(uuid4())
        self.refs[obj_uuid] = (io, obj)
        return obj_uuid


class _ModelUnpickler(Unpickler):
    def __init__(self, refs, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.refs = refs

    def persistent_load(self, pid: str) -> Any:
        return self.refs[pid]


class CallableModelType(ModelType, ModelHook):
    type: ClassVar = "callable"
    priority: ClassVar = LOW_PRIORITY_VALUE

    @classmethod
    def process(
        cls, obj: Callable, sample_data: Optional[Any] = None, **kwargs
    ) -> ModelType:
        s = Signature.from_method(
            obj, sample_data, auto_infer=sample_data is not None
        )
        predict = s.copy()
        predict.name = "predict"
        predict.args[0].name = "data"
        return CallableModelType(
            io=PickleModelIO(), methods={"__call__": s, "predict": predict}
        ).bind(obj)

    @classmethod
    def is_object_valid(cls, obj: Any) -> bool:
        return callable(obj)

    def predict(self, data):
        return self.model(data)


# Copyright 2019 Zyfra
# Copyright 2021 Iterative
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
