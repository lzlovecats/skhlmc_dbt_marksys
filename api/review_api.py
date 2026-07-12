from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import Response as BinaryResponse
from pydantic import BaseModel
router=APIRouter(prefix="/api/review",tags=["review"]); COOKIE="review_match"
class LoginBody(BaseModel): match_id:str; password:str
def db():
 from deploy.proxy import get_vote_db
 return get_vote_db()
def scope(request):
 from deploy.proxy import _verify_review_token
 value=_verify_review_token(request.cookies.get(COOKIE) or "")
 if not value: raise HTTPException(401,"請先驗證查閱分紙密碼。")
 return value
@router.get('/matches')
def matches():
 from core.review_logic import available_matches
 return {"matches":available_matches(db())}
@router.post('/login')
def login(body:LoginBody,response:Response):
 from core.review_logic import verify_review_access
 from deploy.proxy import _sign_review_token
 result=verify_review_access(body.match_id,body.password,db())
 if not result['ok']: raise HTTPException(401,result['message'])
 token=_sign_review_token(body.match_id)
 if not token: raise HTTPException(503,'登入服務暫時未能使用。')
 response.set_cookie(COOKIE,token,path='/',samesite='lax',httponly=True); return {"ok":True}
@router.get('/data')
def data(request:Request,judge_name:str|None=None):
 from core.review_logic import review_data
 return review_data(scope(request),judge_name,db())
@router.post('/logout')
def logout(response:Response): response.delete_cookie(COOKIE,path='/'); return {"ok":True}
