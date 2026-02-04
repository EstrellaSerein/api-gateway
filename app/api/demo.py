from flask import Flask, Response, request, jsonify
import requests

app = Flask(__name__)

@app.route('/model', methods=['POST'])
def model():
    # 大模型服务的新IP地址和端口
    new_model_service_url = 'http://192.168.1.162:11434/api/chat'
    
    # 获取前端发送的数据
    data = request.json
    
    # 转发请求到大模型服务
    response_from_model = requests.post(
        new_model_service_url,
        headers={'Content-Type': 'application/json'},
        json=data,
        stream=True
    )
    
    # 检查响应状态码
    if response_from_model.status_code != 200:
        return f"Error: {response_from_model.status_code}", response_from_model.status_code
    
    # 定义一个生成器函数来处理流式响应
    def generate():
        for chunk in response_from_model.iter_content(chunk_size=8192):
            if chunk:
                yield chunk
    
    # 返回流式响应给前端
    return Response(generate(), content_type=response_from_model.headers['Content-Type'])

if __name__ == '__main__':
    app.run(debug=True)