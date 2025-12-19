# app/job_utils.py

from datetime import datetime
from flask import current_app
from .models import db, BatchJob, JobStatus

def check_job_should_stop(job_id: int) -> bool:
    """
    Cek apakah job harus dihentikan dengan menggunakan fresh query.
    Return: True jika harus stop, False jika lanjut.
    """
    if job_id is None:
        return True # Jika tidak ada job_id, lebih baik berhenti
        
    try:
        # Gunakan with_for_update untuk lock baris dan dapatkan data terbaru
        job = db.session.query(BatchJob).filter_by(id=job_id).with_for_update().first()
        if job:
            return job.status == JobStatus.STOPPING
        return True # Jika job tidak ditemukan, anggap harus berhenti
    except Exception as e:
        current_app.logger.error(f"Error checking job status for job_id {job_id}: {e}")
        return True  # Jika error, lebih baik stop untuk keamanan

def cleanup_job_state(job_name: str, status: JobStatus = JobStatus.IDLE, error_msg: str = None):
    """
    Fungsi helper untuk membersihkan state job dengan aman.
    """
    try:
        job = BatchJob.query.filter_by(job_name=job_name).with_for_update().first()
        if job:
            job.status = status
            if error_msg:
                job.last_error = error_msg
            if status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.IDLE]:
                job.completed_at = datetime.utcnow()
            db.session.commit()
            return True
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to cleanup job state for {job_name}: {e}")
    return False

def update_job_heartbeat(job_id: int, processed_count: int = None, message: str = None):
    """
    Update heartbeat timestamp dan optional progress/message.
    """
    if job_id is None:
        return False
        
    try:
        update_data = {'last_updated': datetime.utcnow()}
        if processed_count is not None:
            update_data['processed_items'] = processed_count
        if message is not None:
            update_data['last_error'] = message
        
        BatchJob.query.filter_by(id=job_id).update(update_data)
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to update heartbeat for job_id {job_id}: {e}")
        return False