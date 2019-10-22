import configparser
from typing import Dict


def get_option_fallback(options: Dict, fallback: Dict):
    '''
    Returns a merged dict with fallback as default values.
    '''
    # Thx: https://thispointer.com/how-to-merge-two-or-more-dictionaries-in-python/
    updated = {**fallback, **options}
    for key in updated.keys():
        try:
            fallback[key]
        except KeyError:
            raise KeyError('key `{}` found, but is not in fallback.'.format(key))
        if type(fallback[key]) == type:
            # a required option
            fallback_type = fallback[key]
            try:
                # it must be specified in options, not in fallback
                value = fallback_type(options[key])
            except KeyError:
                raise KeyError('key `{}` is required'.format(key))
        else:
            fallback_type = type(fallback[key])
            # an option with default
            value = fallback_type(updated[key])
        updated[key] = value
    return updated


class BaseConfig(object):
    '''
    BaseConfig is designed not to be affected by specific model or experiment.
    It provides base functionalities, but not concrete ones.

    最小構成として、config.modelのようなアクセスをtrainのなかで利用できるようにする。
    そのために、まずはdictからclass objectのattributeに変換することで利便性を高める。
    '''

    def __init__(self, options: Dict):
        self._attr_list = list()
        for attr, value in options.items():
            setattr(self, attr, value)
            self._attr_list.append(attr)

    def as_dict(self):
        return {k: getattr(self, k) for k in self._attr_list}


class Config(BaseConfig):
    '''
    Specific functionalities.
    '''

    def setup(self):
        pass
