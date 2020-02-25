//
//  Configuration.swift
//  Gramophone
//
//  Copyright (c) 2017 Jared Verdi. All Rights Reserved
//
//  Permission is hereby granted, free of charge, to any person obtaining a copy
//  of this software and associated documentation files (the "Software"), to deal
//  in the Software without restriction, including without limitation the rights
//  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
//  copies of the Software, and to permit persons to whom the Software is
//  furnished to do so, subject to the following conditions:
//
//  The above copyright notice and this permission notice shall be included in all
//  copies or substantial portions of the Software.
//
//  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
//  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
//  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
//  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
//  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
//  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
//  SOFTWARE.
//

import Foundation

public struct ClientConfiguration {
    let clientID: String
    let redirectURI: String
    
    let scopes: [Scope]
    
    let apiScheme: String
    let apiHost: String

    public init(clientID: String,
                redirectURI: String,
                scopes: [Scope] = [],
                apiScheme: String = "https",
                apiHost: String = "api.instagram.com") {
    
        self.clientID = clientID
        self.redirectURI = redirectURI
        self.scopes = scopes
        self.apiScheme = apiScheme
        self.apiHost = apiHost
    }
}

public enum Scope: String {
    case basic = "basic"
    case publicContent = "public_content"
    case followerList = "follower_list"
    case comments = "comments"
    case relationships = "relationships"
    case likes = "likes"
    
    static let separator = "+"
}
