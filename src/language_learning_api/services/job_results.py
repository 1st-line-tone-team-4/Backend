"""Legacy callback module retired by the canonical durable job integration.

Result persistence now runs only via registered GradeHandler and TeachbackHandler
instances under app.jobs.JobService lease fencing. There are deliberately no public
complete_* callbacks: a job ID cannot establish a current lease, and an internal
commit would split atomic job/domain persistence. Import handlers from grading
or teachback_handlers instead.
"""
