import os,sys,tempfile
os.environ['DATABASE_URL']='sqlite:///'+os.path.join(tempfile.mkdtemp(prefix='coretest'),'test.db')
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
