"""Tests for wine-label image handling: content-block building + Slack download."""

from unittest.mock import MagicMock, patch

import handlers
import nlp


class TestBuildUserContent:
    def test_text_only_returns_plain_string(self):
        assert nlp._build_user_content("who owns this?", None) == "who owns this?"
        assert nlp._build_user_content("hi", []) == "hi"

    def test_image_with_caption_builds_blocks(self):
        content = nlp._build_user_content(
            "whose wine?", [{"media_type": "image/png", "data": "BBBB"}]
        )
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "whose wine?"}
        assert content[1]["type"] == "image"
        assert content[1]["source"] == {
            "type": "base64", "media_type": "image/png", "data": "BBBB"
        }

    def test_image_only_synthesizes_caption(self):
        content = nlp._build_user_content("", [{"media_type": "image/jpeg", "data": "AAAA"}])
        assert content[0]["type"] == "text"
        assert content[0]["text"].strip()  # non-empty synthesized caption
        assert "label" in content[0]["text"].lower()

    def test_multiple_images(self):
        content = nlp._build_user_content(
            "x",
            [
                {"media_type": "image/jpeg", "data": "A"},
                {"media_type": "image/webp", "data": "B"},
            ],
        )
        assert sum(1 for b in content if b["type"] == "image") == 2


class TestExtractImages:
    def _client(self):
        c = MagicMock()
        c.token = "xoxb-test"
        return c

    def test_no_files_returns_empty(self):
        assert handlers._extract_images({}, self._client()) == []
        assert handlers._extract_images({"files": []}, self._client()) == []

    def test_skips_non_image_files(self):
        event = {"files": [{"mimetype": "application/pdf", "url_private_download": "u",
                            "size": 100}]}
        assert handlers._extract_images(event, self._client()) == []

    def test_skips_oversized_images(self):
        event = {"files": [{"mimetype": "image/jpeg", "url_private_download": "u",
                            "size": handlers._MAX_IMAGE_BYTES + 1}]}
        assert handlers._extract_images(event, self._client()) == []

    @patch("handlers._no_redirect_opener.open")
    def test_downloads_and_base64_encodes(self, mock_open):
        resp = MagicMock()
        resp.read.return_value = b"\xff\xd8\xff\xe0imagebytes"
        mock_open.return_value.__enter__.return_value = resp

        event = {"files": [{"mimetype": "image/jpeg", "size": 11,
                            "url_private_download": "https://files.slack.com/x.jpg"}]}
        images = handlers._extract_images(event, self._client())

        assert len(images) == 1
        assert images[0]["media_type"] == "image/jpeg"
        import base64
        assert base64.standard_b64decode(images[0]["data"]) == b"\xff\xd8\xff\xe0imagebytes"
        # Auth header carries the bot token.
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer xoxb-test"

    @patch("handlers._no_redirect_opener.open")
    def test_caps_image_count(self, mock_open):
        resp = MagicMock()
        resp.read.return_value = b"img"
        mock_open.return_value.__enter__.return_value = resp

        files = [
            {"mimetype": "image/png", "size": 3,
             "url_private_download": f"https://files.slack.com/img{i}.png"}
            for i in range(handlers._MAX_IMAGES + 2)
        ]
        images = handlers._extract_images({"files": files}, self._client())
        assert len(images) == handlers._MAX_IMAGES

    @patch("handlers._no_redirect_opener.open", side_effect=Exception("network"))
    def test_download_failure_is_non_fatal(self, _mock):
        event = {"files": [{"mimetype": "image/jpeg", "size": 5,
                            "url_private_download": "https://files.slack.com/x.jpg"}]}
        assert handlers._extract_images(event, self._client()) == []

    @patch("handlers._no_redirect_opener.open")
    def test_skips_non_slack_host(self, mock_open):
        # SSRF guard: never send the bot token to a non-Slack host.
        event = {"files": [{"mimetype": "image/jpeg", "size": 5,
                            "url_private_download": "https://evil.example.com/x.jpg"}]}
        assert handlers._extract_images(event, self._client()) == []
        mock_open.assert_not_called()

    @patch("handlers._no_redirect_opener.open")
    def test_skips_non_https_slack_url(self, mock_open):
        event = {"files": [{"mimetype": "image/jpeg", "size": 5,
                            "url_private_download": "http://files.slack.com/x.jpg"}]}
        assert handlers._extract_images(event, self._client()) == []
        mock_open.assert_not_called()

    def test_is_slack_host_matrix(self):
        assert handlers._is_slack_host("https://files.slack.com/a.jpg")
        assert handlers._is_slack_host("https://x.slack-edge.com/a.jpg")
        assert not handlers._is_slack_host("https://evil.com/a.jpg")
        assert not handlers._is_slack_host("https://notslack.com.evil.com/a.jpg")
        assert not handlers._is_slack_host("http://files.slack.com/a.jpg")
