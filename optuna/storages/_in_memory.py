import copy
from datetime import datetime
import re
import threading
from typing import Any
from typing import cast
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Union
import uuid

import optuna
from optuna import distributions  # NOQA
from optuna.exceptions import DuplicatedStudyError
from optuna.storages import BaseStorage
from optuna.storages._base import DEFAULT_STUDY_NAME_PREFIX
from optuna.study._study_direction import StudyDirection
from optuna.study._study_summary import StudySummary
from optuna.trial import FrozenTrial
from optuna.trial import TrialState


_logger = optuna.logging.get_logger(__name__)


class InMemoryStorage(BaseStorage):
    """Storage class that stores data in memory of the Python process.

    This class is not supposed to be directly accessed by library users.
    """

    def __init__(self) -> None:
        self._trial_id_to_study_id_and_number: Dict[int, Tuple[int, int]] = {}
        self._study_name_to_id: Dict[str, int] = {}
        self._studies: Dict[int, _StudyInfo] = {}

        self._max_study_id = -1
        self._max_trial_id = -1

        self._lock = threading.RLock()

    def __getstate__(self) -> Dict[Any, Any]:
        state = self.__dict__.copy()
        del state["_lock"]
        return state

    def __setstate__(self, state: Dict[Any, Any]) -> None:
        self.__dict__.update(state)
        self._lock = threading.RLock()

    def create_new_study(self, study_name: Optional[str] = None) -> int:

        with self._lock:
            study_id = self._max_study_id + 1
            self._max_study_id += 1

            if study_name is not None:
                if study_name in self._study_name_to_id:
                    raise DuplicatedStudyError
            else:
                study_uuid = str(uuid.uuid4())
                study_name = DEFAULT_STUDY_NAME_PREFIX + study_uuid
            self._studies[study_id] = _StudyInfo(study_name)
            self._study_name_to_id[study_name] = study_id

            _logger.info("A new study created in memory with name: {}".format(study_name))

            return study_id

    def delete_study(self, study_id: int) -> None:

        with self._lock:
            self._check_study_id(study_id)

            for trial in self._studies[study_id].trials:
                del self._trial_id_to_study_id_and_number[trial._trial_id]
            study_name = self._studies[study_id].name
            del self._study_name_to_id[study_name]
            del self._studies[study_id]

    def set_study_directions(self, study_id: int, directions: Sequence[StudyDirection]) -> None:

        with self._lock:
            self._check_study_id(study_id)

            study = self._studies[study_id]
            if study.directions[0] != StudyDirection.NOT_SET and study.directions != list(
                directions
            ):
                raise ValueError(
                    "Cannot overwrite study direction from {} to {}.".format(
                        study.directions, directions
                    )
                )
            study.directions = list(directions)

    def set_study_user_attr(self, study_id: int, key: str, value: Any) -> None:

        with self._lock:
            self._check_study_id(study_id)

            self._studies[study_id].user_attrs[key] = value

    def set_study_system_attr(self, study_id: int, key: str, value: Any) -> None:

        with self._lock:
            self._check_study_id(study_id)

            self._studies[study_id].system_attrs[key] = value

    def get_study_id_from_name(self, study_name: str) -> int:

        with self._lock:
            if study_name not in self._study_name_to_id:
                raise KeyError("No such study {}.".format(study_name))

            return self._study_name_to_id[study_name]

    def get_study_id_from_trial_id(self, trial_id: int) -> int:

        with self._lock:
            self._check_trial_id(trial_id)

            return self._trial_id_to_study_id_and_number[trial_id][0]

    def get_study_name_from_id(self, study_id: int) -> str:

        with self._lock:
            self._check_study_id(study_id)
            return self._studies[study_id].name

    def get_study_directions(self, study_id: int) -> List[StudyDirection]:

        with self._lock:
            self._check_study_id(study_id)
            return self._studies[study_id].directions

    def get_study_user_attrs(self, study_id: int) -> Dict[str, Any]:

        with self._lock:
            self._check_study_id(study_id)
            return self._studies[study_id].user_attrs

    def get_study_system_attrs(self, study_id: int) -> Dict[str, Any]:

        with self._lock:
            self._check_study_id(study_id)
            return self._studies[study_id].system_attrs

    def get_all_study_summaries(self, regex: str = None) -> List[StudySummary]:

        with self._lock:
            if regex is not None:
                pattern = re.compile(regex)
                studies = [
                    study_id
                    for study_id, study in self._studies.items()
                    if pattern.match(study.name)
                ]
            else:
                studies = list(self._studies.keys())
            return [self._build_study_summary(study_id) for study_id in studies]

    def _build_study_summary(self, study_id: int) -> StudySummary:
        study = self._studies[study_id]
        return StudySummary(
            study_name=study.name,
            direction=None,
            directions=study.directions,
            best_trial=copy.deepcopy(self._get_trial(study.best_trial_id))
            if study.best_trial_id is not None
            else None,
            user_attrs=copy.deepcopy(study.user_attrs),
            system_attrs=copy.deepcopy(study.system_attrs),
            n_trials=len(study.trials),
            datetime_start=min(
                [
                    cast(datetime, trial.datetime_start)
                    for trial in self.get_all_trials(study_id, deepcopy=False)
                    if trial.datetime_start is not None
                ],
                default=None,
            )
            if study.trials
            else None,
            study_id=study_id,
        )

    def create_new_trial(self, study_id: int, template_trial: Optional[FrozenTrial] = None) -> int:

        with self._lock:
            self._check_study_id(study_id)

            if template_trial is None:
                trial = self._create_running_trial()
            else:
                trial = copy.deepcopy(template_trial)

            trial_id = self._max_trial_id + 1
            self._max_trial_id += 1
            trial.number = len(self._studies[study_id].trials)
            trial._trial_id = trial_id
            self._trial_id_to_study_id_and_number[trial_id] = (study_id, trial.number)
            self._studies[study_id].trials.append(trial)
            self._update_cache(trial_id, study_id)
            return trial_id

    @staticmethod
    def _create_running_trial() -> FrozenTrial:

        return FrozenTrial(
            trial_id=-1,  # dummy value.
            number=-1,  # dummy value.
            state=TrialState.RUNNING,
            params={},
            distributions={},
            user_attrs={},
            system_attrs={},
            value=None,
            intermediate_values={},
            datetime_start=datetime.now(),
            datetime_complete=None,
        )

    def set_trial_state(self, trial_id: int, state: TrialState) -> bool:

        with self._lock:
            trial = self._get_trial(trial_id)
            self.check_trial_is_updatable(trial_id, trial.state)

            trial = copy.copy(trial)
            self.check_trial_is_updatable(trial_id, trial.state)

            if state == TrialState.RUNNING and trial.state != TrialState.WAITING:
                return False

            trial.state = state

            if state == TrialState.RUNNING:
                trial.datetime_start = datetime.now()

            if state.is_finished():
                trial.datetime_complete = datetime.now()
                self._set_trial(trial_id, trial)
                study_id = self._trial_id_to_study_id_and_number[trial_id][0]
                self._update_cache(trial_id, study_id)
            else:
                self._set_trial(trial_id, trial)

            return True

    def set_trial_param(
        self,
        trial_id: int,
        param_name: str,
        param_value_internal: float,
        distribution: distributions.BaseDistribution,
    ) -> None:

        with self._lock:
            trial = self._get_trial(trial_id)

            self.check_trial_is_updatable(trial_id, trial.state)

            study_id = self._trial_id_to_study_id_and_number[trial_id][0]
            # Check param distribution compatibility with previous trial(s).
            if param_name in self._studies[study_id].param_distribution:
                distributions.check_distribution_compatibility(
                    self._studies[study_id].param_distribution[param_name], distribution
                )

            # Set param distribution.
            self._studies[study_id].param_distribution[param_name] = distribution

            # Set param.
            trial = copy.copy(trial)
            trial.params = copy.copy(trial.params)
            trial.params[param_name] = distribution.to_external_repr(param_value_internal)
            trial.distributions = copy.copy(trial.distributions)
            trial.distributions[param_name] = distribution
            self._set_trial(trial_id, trial)

    def get_trial_id_from_study_id_trial_number(self, study_id: int, trial_number: int) -> int:

        with self._lock:
            study = self._studies.get(study_id)
            if study is None:
                raise KeyError("No study with study_id {} exists.".format(study_id))

            trials = study.trials
            if len(trials) <= trial_number:
                raise KeyError(
                    "No trial with trial number {} exists in study with study_id {}.".format(
                        trial_number, study_id
                    )
                )

            trial = trials[trial_number]
            assert trial.number == trial_number

            return trial._trial_id

    def get_trial_number_from_id(self, trial_id: int) -> int:

        with self._lock:
            self._check_trial_id(trial_id)

            return self._trial_id_to_study_id_and_number[trial_id][1]

    def get_best_trial(self, study_id: int) -> FrozenTrial:

        with self._lock:
            self._check_study_id(study_id)

            best_trial_id = self._studies[study_id].best_trial_id
            if best_trial_id is None:
                raise ValueError("No trials are completed yet.")
            elif len(self._studies[study_id].directions) > 1:
                raise ValueError(
                    "Best trial can be obtained only for single-objective optimization."
                )
            return self.get_trial(best_trial_id)

    def get_trial_param(self, trial_id: int, param_name: str) -> float:

        with self._lock:
            trial = self._get_trial(trial_id)

            distribution = trial.distributions[param_name]
            return distribution.to_internal_repr(trial.params[param_name])

    def set_trial_values(self, trial_id: int, values: Sequence[float]) -> None:

        with self._lock:
            trial = self._get_trial(trial_id)
            self.check_trial_is_updatable(trial_id, trial.state)

            trial = copy.copy(trial)
            self.check_trial_is_updatable(trial_id, trial.state)

            trial.values = values
            self._set_trial(trial_id, trial)

    def _update_cache(self, trial_id: int, study_id: int) -> None:

        trial = self._get_trial(trial_id)

        if trial.state != TrialState.COMPLETE:
            return

        best_trial_id = self._studies[study_id].best_trial_id
        if best_trial_id is None:
            self._studies[study_id].best_trial_id = trial_id
            return

        _directions = self.get_study_directions(study_id)
        if len(_directions) > 1:
            return
        direction = _directions[0]

        best_trial = self._get_trial(best_trial_id)
        assert best_trial is not None
        if best_trial.value is None:
            self._studies[study_id].best_trial_id = trial_id
            return
        # Complete trials do not have `None` values.
        assert trial.value is not None
        best_value = best_trial.value
        new_value = trial.value

        if direction == StudyDirection.MAXIMIZE:
            if best_value < new_value:
                self._studies[study_id].best_trial_id = trial_id
        else:
            if best_value > new_value:
                self._studies[study_id].best_trial_id = trial_id

    def set_trial_intermediate_value(
        self, trial_id: int, step: int, intermediate_value: float
    ) -> None:

        with self._lock:
            trial = self._get_trial(trial_id)
            self.check_trial_is_updatable(trial_id, trial.state)

            trial = copy.copy(trial)
            trial.intermediate_values = copy.copy(trial.intermediate_values)
            trial.intermediate_values[step] = intermediate_value
            self._set_trial(trial_id, trial)

    def set_trial_user_attr(self, trial_id: int, key: str, value: Any) -> None:

        with self._lock:
            self._check_trial_id(trial_id)
            trial = self._get_trial(trial_id)
            self.check_trial_is_updatable(trial_id, trial.state)

            self.check_trial_is_updatable(trial_id, trial.state)

            trial = copy.copy(trial)
            trial.user_attrs = copy.copy(trial.user_attrs)
            trial.user_attrs[key] = value
            self._set_trial(trial_id, trial)

    def set_trial_system_attr(self, trial_id: int, key: str, value: Any) -> None:

        with self._lock:
            trial = self._get_trial(trial_id)
            self.check_trial_is_updatable(trial_id, trial.state)

            self.check_trial_is_updatable(trial_id, trial.state)

            trial = copy.copy(trial)
            trial.system_attrs = copy.copy(trial.system_attrs)
            trial.system_attrs[key] = value
            self._set_trial(trial_id, trial)

    def get_trial(self, trial_id: int) -> FrozenTrial:

        with self._lock:
            return self._get_trial(trial_id)

    def _get_trial(self, trial_id: int) -> FrozenTrial:

        self._check_trial_id(trial_id)
        study_id, trial_number = self._trial_id_to_study_id_and_number[trial_id]
        return self._studies[study_id].trials[trial_number]

    def _set_trial(self, trial_id: int, trial: FrozenTrial) -> None:
        study_id, trial_number = self._trial_id_to_study_id_and_number[trial_id]
        self._studies[study_id].trials[trial_number] = trial

    def get_all_trials(
        self,
        study_id: int,
        deepcopy: bool = True,
        states: Optional[Tuple[TrialState, ...]] = None,
    ) -> List[FrozenTrial]:

        with self._lock:
            self._check_study_id(study_id)

            trials = self._studies[
                study_id
            ].trials  # type: Union[List[FrozenTrial], Iterator[FrozenTrial]]

            if states is not None:
                trials = filter(lambda t: t.state in states, trials)

            if deepcopy:
                trials = copy.deepcopy(list(trials))
            else:
                trials = list(trials)

        return trials

    def read_trials_from_remote_storage(self, study_id: int) -> None:
        self._check_study_id(study_id)

    def _check_study_id(self, study_id: int) -> None:

        if study_id not in self._studies:
            raise KeyError("No study with study_id {} exists.".format(study_id))

    def _check_trial_id(self, trial_id: int) -> None:

        if trial_id not in self._trial_id_to_study_id_and_number:
            raise KeyError("No trial with trial_id {} exists.".format(trial_id))


class _StudyInfo:
    def __init__(self, name: str) -> None:
        self.trials: List[FrozenTrial] = []
        self.param_distribution: Dict[str, distributions.BaseDistribution] = {}
        self.user_attrs: Dict[str, Any] = {}
        self.system_attrs: Dict[str, Any] = {}
        self.name: str = name
        self.directions: List[StudyDirection] = [StudyDirection.NOT_SET]
        self.best_trial_id: Optional[int] = None
