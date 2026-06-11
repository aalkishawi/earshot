"""TwiML builder tests — XML safety and Gather/Hangup shape."""
from __future__ import annotations

from earshot.webhook.twiml import gather_response, sanitize_script, say_and_hangup


# --- sanitize_script -------------------------------------------------------- #

def test_break_tags_pass_through():
    assert sanitize_script('hi <break time="500ms"/> there') == 'hi <break time="500ms"/> there'


def test_break_tag_with_seconds():
    assert sanitize_script('a <break time="1s"/> b') == 'a <break time="1s"/> b'


def test_disallowed_tags_dropped_not_escaped():
    """Critical: a script tag must NEVER be read aloud as escaped text."""
    assert sanitize_script('<script>alert(1)</script> safe') == 'alert(1) safe'


def test_ampersand_escaped():
    assert sanitize_script('A & B') == 'A &amp; B'


def test_less_than_escaped():
    assert sanitize_script('1 < 2') == '1 &lt; 2'


def test_empty_input():
    assert sanitize_script('') == ''
    assert sanitize_script(None) == ''


def test_malformed_break_tag_dropped():
    # Missing units — not a valid break tag, drop it
    assert sanitize_script('a <break time="500"/> b') == 'a  b'


# --- gather_response -------------------------------------------------------- #

def test_gather_response_well_formed():
    tw = gather_response(
        "Hello world",
        voice="Polly.Joanna",
        action_url="https://test.example/twiml/turn?interaction_id=5",
    )
    assert tw.startswith('<Response>')
    assert tw.endswith('</Response>')
    assert '<Gather' in tw
    assert 'input="speech"' in tw
    assert '<Say voice="Polly.Joanna">Hello world</Say>' in tw
    assert '</Gather>' in tw
    assert '<Redirect' in tw
    assert 'timeout=true' in tw  # silence fallthrough


def test_gather_response_sanitizes_script():
    tw = gather_response('<script>x</script>', 'Polly.Joanna', 'https://x.example/')
    assert '<script>' not in tw
    assert 'x</Say>' in tw  # dropped tag, content kept


def test_gather_response_rejects_malformed_voice():
    """Voice that fails regex falls back to Polly.Joanna."""
    tw = gather_response('hi', 'evil"><Script>', 'https://x.example/')
    assert 'Polly.Joanna' in tw
    assert 'Script' not in tw


# --- say_and_hangup --------------------------------------------------------- #

def test_say_and_hangup_shape():
    tw = say_and_hangup('Goodbye', 'Polly.Joanna')
    assert tw == '<Response><Say voice="Polly.Joanna">Goodbye</Say><Hangup/></Response>'
