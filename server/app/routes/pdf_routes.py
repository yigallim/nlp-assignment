import os
import hashlib
import threading
from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename
from app.extensions import mongo
from bson import ObjectId
from app.utils.response import clean_res
from flask_cors import cross_origin
from flask import current_app, send_from_directory
from app.utils.pdf_preprocess import PDFUtils
from app.utils.llm_api import LLMApi

pdf_bp = Blueprint('pdf', __name__)

def _async_count_and_update(pdf_id, file_path, app):
    with app.app_context():
        if not os.path.exists(file_path):
            app.logger.warning(f"Count aborted: file {file_path} no longer exists")
            return

        pdf_obj = mongo.db.pdf_files.find_one({'_id': pdf_id})
        if not pdf_obj:
            app.logger.warning(f"Count aborted: PDF record {pdf_id} already deleted")
            return
        try:
            wc = PDFUtils.count_words_pdf(file_path)
        except Exception as e:
            app.logger.error(f"Word count failed for {pdf_id}: {e}")
            wc = 0
        mongo.db.pdf_files.update_one(
            {'_id': pdf_id},
            {'$set': {'word_count': wc}}
        )

def _async_vectorize(pdf_id: str, pdf_path: str, pdf_chunks_collection, app):
    with app.app_context():
        if not os.path.exists(pdf_path):
            app.logger.warning(f"Vectorize aborted: file {pdf_path} no longer exists")
            return
        obj_id = ObjectId(pdf_id)
        pdf_obj = mongo.db.pdf_files.find_one({'_id': obj_id})
        if not pdf_obj:
            app.logger.warning(f"Vectorize aborted: PDF record {pdf_id} already deleted")
            return

        try:
            pdf_info = PDFUtils.extract_information(pdf_path)
            PDFUtils.store_pdf_chunks_to_chroma(pdf_id, pdf_info, pdf_chunks_collection)
        except Exception as e:
            app.logger.error(f"Vectorization failed for {pdf_id}: {e}")
        finally:
            mongo.db.pdf_files.update_one(
                {'_id': obj_id},
                {'$set': {'loading': False}}
            )

@pdf_bp.route('/pdf', methods=['GET'])
def get_all_pdf():
    pdfs = list(mongo.db.pdf_files.find({}, {'hash': 0}))
    cleaned = clean_res(pdfs)
    return jsonify(cleaned)

@pdf_bp.route('/pdf/<pdf_id>', methods=['GET'])
def get_single_pdf(pdf_id):
    try:
        obj_id = ObjectId(pdf_id)
    except Exception:
        return jsonify({'error': 'Invalid PDF ID format'}), 400

    pdf_doc = mongo.db.pdf_files.find_one({'_id': obj_id}, {'hash': 0})
    if not pdf_doc:
        return jsonify({'error': 'PDF not found'}), 404

    cleaned = clean_res([pdf_doc])[0]
    return jsonify(cleaned), 200


@pdf_bp.route('/pdf', methods=['POST'])
def upload_pdf():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part in the request'}), 400

    file = request.files['file']
    if not file or not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Only PDF files are allowed'}), 400

    file_content = file.read()
    file_hash = hashlib.sha256(file_content).hexdigest()

    existing = mongo.db.pdf_files.find_one({'hash': file_hash})
    if existing:
        return jsonify({
            'id': str(existing['_id']),
            'filename': existing.get('filename', ''),
            'existed': True
        }), 201

    pdf_doc = {
        'filename': secure_filename(file.filename),
        'hash': file_hash,
        'word_count': -1,
        'loading': True 
    }
    pdf_id = mongo.db.pdf_files.insert_one(pdf_doc).inserted_id

    upload_folder = os.path.join(current_app.root_path, '..', 'uploads')
    os.makedirs(upload_folder, exist_ok=True)
    saved_path = os.path.join(upload_folder, f"{pdf_id}.pdf")
    with open(saved_path, 'wb') as f:
        f.write(file_content)

    threading.Thread(
        target=_async_count_and_update,
        args=(pdf_id, saved_path, current_app._get_current_object()),
        daemon=True
    ).start()

    threading.Thread(
        target=_async_vectorize,
        args=(str(pdf_id), saved_path, current_app.pdf_chunks_collection, current_app._get_current_object()),
        daemon=True
    ).start()

    return jsonify({
        'id': str(pdf_id),
        'filename': pdf_doc['filename'],
        'existed': False
    }), 201

@pdf_bp.route('/pdf', methods=['DELETE'])
def delete_pdf():
    data = request.get_json()
    if not data or 'id' not in data:
        return jsonify({'error': 'Missing id'}), 400

    try:
        pdf_id = ObjectId(data['id'])
    except Exception:
        return jsonify({'error': 'Invalid id format'}), 400

    pdf_doc = mongo.db.pdf_files.find_one({'_id': pdf_id})
    if not pdf_doc:
        return jsonify({'error': 'PDF not found'}), 404

    referencing_convs = list(
        mongo.db.conversations.find({'pdfMeta.id': str(pdf_id)}, {'_id': 1, 'label': 1})
    )

    if referencing_convs:
        conflicted = [{'id': str(c['_id']), 'label': c.get('label', '')}
                      for c in referencing_convs]
        return (
            jsonify({
                'error': 'This PDF is linked to existing conversation(s) '
                         'and cannot be deleted.',
                'linked_conversations': conflicted
            }),
            409  
        )
    filename = f"{pdf_id}.pdf"
    upload_folder = os.path.join(os.getcwd(), 'uploads')
    file_path = os.path.join(upload_folder, filename)
    if os.path.exists(file_path):
        os.remove(file_path)

    current_app.pdf_chunks_collection.delete(where={"pdf_id": str(pdf_id)})
    mongo.db.pdf_files.delete_one({'_id': pdf_id})
    return jsonify({'deleted_id': data['id']}), 200

@pdf_bp.route('/uploads/<path:filename>')
@cross_origin()
def uploaded_file(filename):
    uploads_dir = os.path.join(current_app.root_path, '..', 'uploads')
    return send_from_directory(uploads_dir, filename)

@pdf_bp.route('/pdf/<pdf_id>/summarize', methods=['POST'])
def summarize_pdf(pdf_id):
    try:
        obj_id = ObjectId(pdf_id)
    except Exception:
        return jsonify({'error': 'Invalid PDF ID format'}), 400

    pdf_doc = mongo.db.pdf_files.find_one({'_id': obj_id})
    if not pdf_doc:
        return jsonify({'error': 'PDF not found'}), 404

    existing_summary = pdf_doc.get('summary')
    if existing_summary:
        return jsonify({
            'summary': existing_summary,
            'pdf_id': pdf_id,
            'cached': True
        }), 200

    if pdf_doc.get('summarizing', False):
        return jsonify({'error': 'This PDF is currently being summarized'}), 409

    mongo.db.pdf_files.update_one({'_id': obj_id}, {'$set': {'summarizing': True}})

    filename = f"{pdf_id}.pdf"
    upload_folder = os.path.join(current_app.root_path, '..', 'uploads')
    file_path = os.path.join(upload_folder, filename)

    if not os.path.exists(file_path):
        mongo.db.pdf_files.update_one({'_id': obj_id}, {'$unset': {'summarizing': ""}})
        return jsonify({'error': 'PDF file not found on server'}), 404

    try:
        summary = LLMApi.summarize_pdf_with_gemini(file_path)
    except Exception as e:
        mongo.db.pdf_files.update_one({'_id': obj_id}, {'$unset': {'summarizing': ""}})
        return jsonify({'error': f'Failed to summarize PDF: {str(e)}'}), 500

    mongo.db.pdf_files.update_one(
        {'_id': obj_id},
        {
            '$set': {'summary': summary},
            '$unset': {'summarizing': ""}
        }
    )

    return jsonify({
        'summary': summary,
        'pdf_id': pdf_id,
        'cached': False
    }), 200
