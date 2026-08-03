from pipeline.render import markdown_to_html


def test_headings_render():
    html = markdown_to_html("# Title\n\n## Subtitle\n")
    assert "<h1>Title</h1>" in html
    assert "<h2>Subtitle</h2>" in html


def test_bullet_list_renders():
    html = markdown_to_html("- first\n- second\n")
    assert "<ul>" in html
    assert "<li>first</li>" in html
    assert "<li>second</li>" in html
    assert "</ul>" in html


def test_numbered_list_renders():
    html = markdown_to_html("1. first\n2. second\n")
    assert "<ol>" in html
    assert "<li>first</li>" in html


def test_code_block_renders_and_escapes():
    html = markdown_to_html('```json\n{"a": "<b>"}\n```\n')
    assert "<pre><code>" in html
    assert "&lt;b&gt;" in html  # escaped, not rendered as a live tag
    assert "</code></pre>" in html


def test_bold_italic_inline_code():
    html = markdown_to_html("This is **bold**, *italic*, and `code`.")
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<code>code</code>" in html


def test_html_escapes_paragraph_text():
    html = markdown_to_html("5 < 10 & true")
    assert "&lt; 10" in html
    assert "&amp;" in html


def test_full_document_wrapper():
    html = markdown_to_html("# X")
    assert html.startswith("<!doctype html>")
    assert "<body>" in html and "</body>" in html
