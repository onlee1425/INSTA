"""
Render 배포용 v3.0
- Gunicorn으로 실행
- 프론트엔드(index.html) 직접 서빙
- 인스타그램 세션 파일 경로 설정
"""
from flask import Flask, request, jsonify, send_file, after_this_request, render_template
from flask_cors import CORS
import instaloader
import os
import tempfile
import shutil
from pathlib import Path
import logging
import requests

logging.basicConfig(level=logging.INFO)

# 정적 파일(HTML)과 템플릿 경로를 현재 위치 기준으로 설정
app = Flask(__name__, static_folder='.', template_folder='.')
CORS(app, expose_headers='Content-Disposition')

# Render는 /var/task 경로에서 실행될 수 있으므로, 세션 파일 경로를 명시적으로 지정
# ★★★ 중요: 여기에 본인 아이디를 입력하고, 이 이름으로 된 세션 파일을 GitHub에 업로드해야 합니다 ★★★
INSTAGRAM_USERNAME = "여기에_인스타그램_아이디_입력"

L = instaloader.Instaloader(
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False
)

try:
    # GitHub에 업로드된 세션 파일을 직접 로드 시도
    logging.info(f"세션 파일로 로그인 시도: {INSTAGRAM_USERNAME}")
    L.load_session_from_file(INSTAGRAM_USERNAME)
    logging.info("세션 파일로 로그인 성공!")
except FileNotFoundError:
    logging.warning(f"세션 파일({INSTAGRAM_USERNAME})을 찾을 수 없습니다. 로그인 없이 진행합니다.")
    logging.warning("배포가 제대로 되려면 GitHub에 세션 파일을 꼭 업로드해주세요.")


def extract_shortcode(url):
    """URL에서 shortcode 추출"""
    if '/p/' in url or '/reel/' in url:
        parts = url.strip('/').split('/')
        if len(parts) >= 2:
            return parts[-1] if '?' not in parts[-1] else parts[-1].split('?')[0]
    return None

# ★★★ 중요: 프론트엔드를 서빙하는 경로 ★★★
@app.route('/')
def serve_frontend():
    """index.html 파일을 렌더링하여 사용자에게 보여줍니다."""
    return render_template('index.html')

@app.route('/api/proxy')
def proxy_image():
    """이미지/영상 미리보기 프록시"""
    url = request.args.get('url')
    if not url:
        return 'URL이 필요합니다.', 400
    try:
        decoded_url = requests.utils.unquote(url)
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(decoded_url, stream=True, headers=headers)
        response.raise_for_status()
        
        def generate():
            for chunk in response.iter_content(chunk_size=8192):
                yield chunk
        
        return app.response_class(generate(), content_type=response.headers['Content-Type'])
    except requests.exceptions.RequestException as e:
        logging.error(f"프록시 요청 실패: {e}")
        return f"프록시 요청 실패: {e}", 500

@app.route('/api/extract', methods=['POST'])
def extract_media():
    """게시물에서 미디어 정보 추출"""
    try:
        data = request.json
        url = data.get('url', '')
        shortcode = extract_shortcode(url)
        if not shortcode:
            return jsonify({'error': '올바른 인스타그램 URL이 아닙니다.'}), 400
        
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        caption_text = post.caption if post.caption else ''
        hashtags = post.caption_hashtags
        main_caption = caption_text
        for tag in hashtags:
            main_caption = main_caption.replace(f"#{tag}", "").strip()

        media_list = []
        nodes = [post] if post.typename != 'GraphSidecar' else post.get_sidecar_nodes()
        for i, node in enumerate(nodes):
            item = {'type': 'image', 'url': node.display_url, 'index': i}
            if node.is_video:
                item['type'] = 'video'
                item['video_url'] = node.video_url
            media_list.append(item)
            
        return jsonify({
            'success': True,
            'post_id': shortcode,
            'caption': main_caption,
            'hashtags': hashtags,
            'media_count': len(media_list),
            'media': media_list
        })
    except Exception as e:
        logging.error(f"추출 오류 발생: {e}")
        return jsonify({'error': f'추출 중 오류가 발생했습니다: {str(e)}'}), 500

@app.route('/api/download', methods=['POST'])
def download_media():
    """개별 미디어 다운로드"""
    temp_dir = tempfile.mkdtemp()
    @after_this_request
    def cleanup(response):
        shutil.rmtree(temp_dir, ignore_errors=True)
        return response
    try:
        data = request.json
        media_url = data.get('url', '')
        shortcode = data.get('shortcode', '')
        index = data.get('index', 0)
        if not media_url or not shortcode:
            return jsonify({'error': '필수 정보가 누락되었습니다.'}), 400
        
        is_video = '.mp4' in media_url.split('?')[0]
        ext = '.mp4' if is_video else '.jpg'
        
        response = requests.get(media_url, stream=True)
        response.raise_for_status()
        filename = f"instagram_{shortcode}_{index+1}{ext}"
        file_path = os.path.join(temp_dir, filename)
        with open(file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return send_file(file_path, as_attachment=True, download_name=filename)
    except Exception as e:
        logging.error(f"개별 다운로드 오류: {e}")
        return jsonify({'error': f'다운로드 오류: {str(e)}'}), 500

@app.route('/api/download_all', methods=['POST'])
def download_all():
    """모든 미디어를 ZIP으로 다운로드"""
    base_temp_dir = tempfile.mkdtemp()
    @after_this_request
    def cleanup(response):
        shutil.rmtree(base_temp_dir, ignore_errors=True)
        return response
    try:
        data = request.json
        url = data.get('url', '')
        shortcode = extract_shortcode(url)
        if not shortcode:
            return jsonify({'error': '올바른 인스타그램 URL이 아닙니다.'}), 400
        
        content_dir = os.path.join(base_temp_dir, 'content')
        os.makedirs(content_dir)
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        L.dirname_pattern = content_dir
        L.filename_pattern = "{profile}_{shortcode}_{date_utc}"
        L.download_post(post, target=Path(content_dir))
        
        zip_base_name = os.path.join(base_temp_dir, f'{shortcode}_all')
        shutil.make_archive(zip_base_name, 'zip', content_dir)
        zip_path = f"{zip_base_name}.zip"
        return send_file(zip_path, as_attachment=True, download_name=f'instagram_{shortcode}.zip')
    except Exception as e:
        logging.error(f"전체 다운로드(ZIP) 오류: {e}")
        return jsonify({'error': f'전체 다운로드 오류: {str(e)}'}), 500

# Render는 gunicorn으로 직접 app을 실행하므로 이 부분은 로컬 테스트용으로만 사용됩니다.
if __name__ == '__main__':
    app.run(debug=True, port=5000)

