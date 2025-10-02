"""
Render 배포용 v5.0 (최종 로그인 해결)
- 세션 파일 방식 대신, Render 환경 변수를 사용하여 서버가 직접 로그인하는 방식으로 교체.
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

app = Flask(__name__, static_folder='.', template_folder='.')
CORS(app, expose_headers='Content-Disposition')

# --- Render 환경 변수에서 로그인 정보 가져오기 ---
INSTA_USER = os.environ.get('INSTA_USER')
INSTA_PASS = os.environ.get('INSTA_PASS')

L = instaloader.Instaloader(
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False
)

# 환경 변수가 존재할 경우, 서버가 직접 로그인 시도
if INSTA_USER and INSTA_PASS:
    try:
        logging.info(f"환경 변수를 사용하여 '{INSTA_USER}' 계정으로 로그인 시도...")
        L.login(INSTA_USER, INSTA_PASS)
        logging.info(f"'{INSTA_USER}' 계정으로 로그인 성공!")
    except Exception as e:
        logging.error(f"환경 변수를 사용한 로그인에 실패했습니다: {e}")
        logging.warning("로그인 없이 진행합니다. 기능이 제한될 수 있습니다.")
else:
    logging.warning("INSTA_USER 또는 INSTA_PASS 환경 변수가 설정되지 않았습니다. 로그인 없이 진행합니다.")

def extract_shortcode(url):
    if '/p/' in url or '/reel/' in url:
        parts = url.strip('/').split('/')
        return parts[-1].split('?')[0] if parts else None
    return None

@app.route('/')
def serve_frontend():
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
        
        caption_text = post.caption or ''
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

if __name__ == '__main__':
    app.run(debug=True, port=5000)

