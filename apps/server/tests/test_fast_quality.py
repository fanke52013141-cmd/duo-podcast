import pytest
from duocast.services.fast_quality import quality_options
from duocast.services.render_inputs import validate_render_options
from duocast.domain.project import Project

def test_quality_levels_have_bounded_sharpening_and_lower_compression():
    assert quality_options('original')['filter']==''
    assert quality_options('crisp')['crf'] < quality_options('clear')['crf'] < quality_options('original')['crf']
    for value in ('bad',None,{},['clear'],17):
        with pytest.raises(ValueError): quality_options(value)

def test_invalid_quality_is_rejected_before_scheduling():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        validate_render_options(Project(id='test'),{'fastQuality':'unknown'},real_video=False)
    assert error.value.status_code==422
